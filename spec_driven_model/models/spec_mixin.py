# Copyright 2019-TODAY Akretion - Raphael Valyi <raphael.valyi@akretion.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.en.html).

from importlib import import_module

from odoo import api, models
from odoo.tools import mute_logger

try:
    # Odoo 19+: moved to odoo.orm.model_classes and renamed.
    from odoo.orm.model_classes import is_model_definition as is_definition_class
except ImportError:
    # Odoo <= 18: original location/name.
    from odoo.models import is_definition_class

try:
    # Odoo 19+: Registry.load() no longer calls the per-model
    # _build_model(pool, cr) classmethod hook (see SpecModel._spec_pre_build's
    # docstring); it calls this module-level function directly instead. Also,
    # BaseModel._prepare_setup()/._setup_base()/._setup_fields() became
    # module-level functions taking a model class instead of instance methods,
    # and ._setup_complete() was renamed to the instance method
    # ._post_model_setup__().
    from odoo.orm import model_classes as _odoo_model_classes
except ImportError:
    _odoo_model_classes = None

from .spec_models import SPEC_MIXIN_MAPPINGS, SpecModel, StackedModel


def _module_to_models_registry():
    """
    Odoo 19 renamed MetaModel.module_to_models to MetaModel._module_to_models__.
    Return a reference to the (mutable, shared) dict either way.
    """
    if hasattr(models.MetaModel, "_module_to_models__"):
        return models.MetaModel._module_to_models__
    return models.MetaModel.module_to_models


def _build_and_register_model(model_type, registry, cr):
    """
    Fully register a dynamically created model class (used to concretize
    "remaining" spec mixins not injected into any existing model), the way
    Registry.load() would for a model defined in a module's source code.
    """
    if _odoo_model_classes is not None:  # Odoo 19+
        return _odoo_model_classes.add_to_registry(registry, model_type)
    return model_type._build_model(registry, cr)  # Odoo <= 18


def _finish_model_setup(env, model_name):
    """
    Force a not-yet-fully-setup model to complete its setup, the way
    setup_model_classes() would for every model after all modules load.
    """
    if _odoo_model_classes is not None:  # Odoo 19+
        model_cls = env.registry[model_name]
        _odoo_model_classes._prepare_setup(model_cls)
        _odoo_model_classes._setup(model_cls, env)
        _odoo_model_classes._setup_fields(model_cls, env)
        model_cls(env, (), ())._post_model_setup__()
    else:  # Odoo <= 18
        env[model_name]._prepare_setup()
        env[model_name]._setup_base()
        env[model_name]._setup_fields()
        env[model_name]._setup_complete()


class SpecMixin(models.AbstractModel):
    """
    This is the root "spec" mixin that will be injected dynamically as the parent
    of your custom schema mixin (such as spec.mixin.nfe) without the need that
    your spec mixin depend on this mixin and on the spec_driven_model module directly
    (loose coupling).
    This root mixin is typically injected via the _build_model method from SpecModel
    or StackedModel that you will be using to inject some spec mixins into
    existing Odoo objects. spec.mixin provides generic utility methods such as a
    _register_hook, import and export methods.
    """

    _description = "root abstract model meant for xsd generated fiscal models"
    _name = "spec.mixin"
    _inherit = ["spec.mixin_export", "spec.mixin_import"]
    _is_spec_driven = True

    def _valid_field_parameter(self, field, name):
        if name in (
            "xsd_type",
            "xsd_required",
            "choice",
            "xsd_choice_required",
            "xsd_implicit",
            "original_comodel_name",
        ):
            return True
        else:
            return super()._valid_field_parameter(field, name)

    @api.model
    def _get_concrete_model(self, model_name):
        "Lookup for concrete models where abstract schema mixins were injected"
        if SPEC_MIXIN_MAPPINGS[self.env.cr.dbname].get(model_name) is not None:
            return self.env[SPEC_MIXIN_MAPPINGS[self.env.cr.dbname].get(model_name)]
        else:
            return self.env.get(model_name)

    def _spec_prefix(self, split=False):
        """
        Get spec_schema and spec_version from context or from class module
        """
        if self._context.get("spec_schema") and self._context.get("spec_version"):
            spec_schema = self._context.get("spec_schema")
            spec_version = self._context.get("spec_version")
            if spec_schema and spec_version:
                spec_version = spec_version.replace(".", "")[:2]
                if split:
                    return spec_schema, spec_version
                return f"{spec_schema}{spec_version}"

        for ancestor in type(self).mro():
            if not ancestor.__module__.startswith("odoo.addons."):
                continue
            mod = import_module(".".join(ancestor.__module__.split(".")[:-1]))
            if hasattr(mod, "spec_schema"):
                spec_schema = mod.spec_schema
                spec_version = mod.spec_version.replace(".", "")[:2]
                if split:
                    return spec_schema, spec_version
                return f"{spec_schema}{spec_version}"

        return None, None if split else None

    def _get_spec_property(self, spec_property="", fallback=None):
        """
        Used to access schema wise and version wise automatic mappings properties
        """
        return getattr(self, f"_{self._spec_prefix()}_{spec_property}", fallback)

    def _get_stacking_points(self):
        return self._get_spec_property("stacking_points", {})

    def _register_hook(self):
        res = super()._register_hook()
        self._register_remaining_schema_models_hook()
        return res

    def _register_remaining_schema_models_hook(self):
        """
        Called once all modules are loaded.
        Here we take all spec models that were not injected into existing concrete
        Odoo models and we make them concrete automatically with
        their _auto_init method that will create their SQL DDL structure.
        """
        spec_schema, spec_version = self._spec_prefix(split=True)
        if not spec_schema:
            return

        load_key = f"_{spec_schema}_register_hook_loaded"
        if hasattr(self.env.registry, load_key):  # hook already called for registry
            return
        setattr(self.env.registry, load_key, True)

        access_data = []
        access_fields = []
        field_prefix = f"{spec_schema}{spec_version}"
        relation_prefix = f"{spec_schema}.{spec_version}.%"
        self.env.cr.execute(
            """SELECT DISTINCT relation FROM ir_model_fields
                   WHERE relation LIKE %s;""",
            (relation_prefix,),
        )
        # now we will filter only the spec models not injected into some existing class:
        remaining_models = {
            i[0]
            for i in self.env.cr.fetchall()
            if self.env.registry.get(i[0])
            and not SPEC_MIXIN_MAPPINGS[self.env.cr.dbname].get(i[0])
        }
        spec_module = self._get_spec_property("odoo_module")
        if "_spec." in spec_module:
            odoo_module = spec_module.split("_spec.")[0].split(".")[-1]
        else:  # for tests:
            odoo_module = "spec_driven_model"
        # concrete classes we build below, tracked so we can drop them
        # from module_to_models at the end of this method
        concrete_models = []
        for name in remaining_models:
            spec_class = StackedModel._odoo_name_to_class(name, spec_module)
            if spec_class is None:
                continue
            # By the time this hook runs, all modules extending this spec
            # mixin via _inherit (e.g. custom fields added by a downstream
            # *_nfe module) have already been merged by Odoo into the
            # registry class for `name`. spec_class only reflects the single
            # class literally defined in spec_module though, so using it
            # alone as the base below would silently drop those extra
            # fields. Pull in every genuine definition class that
            # contributed to the merged registry class (skipping registry
            # ("NewClass") wrappers themselves, which cannot safely be reused
            # as a base for another _build_model() call).
            merged_class = self.env.registry[name]
            # accessed via getattr to avoid Python's name mangling of the
            # double-underscore "__base_classes" attribute set by Odoo's
            # BaseModel._build_model(). Odoo 19 renamed this attribute to
            # "_base_classes__" (no mangling).
            merged_base_classes = getattr(
                merged_class,
                "_base_classes__",
                getattr(merged_class, "_BaseModel__base_classes", None),
            )
            definition_bases = tuple(
                base for base in merged_base_classes if is_definition_class(base)
            )
            fields = merged_class._fields
            rec_name = next(
                filter(
                    lambda x: (x.startswith(field_prefix) and "_choice" not in x),
                    fields,
                ),
                None,
            )
            model_type = type(
                name,
                (SpecModel,) + definition_bases,
                {
                    "_name": name,
                    "_inherit": spec_class._inherit,
                    "_original_module": odoo_module,
                    "_rec_name": rec_name,
                    "_module": odoo_module,
                },
            )
            # we set _spec_schema and _spec_version because
            # _build_model will not have context access:
            # In Odoo 18+, the test framework monitors model attribute modifications
            # and logs stack traces. We suppress these during dynamic model building.
            with mute_logger("odoo.tests.common"):
                model_type._spec_schema = spec_schema
                model_type._spec_version = spec_version
            _module_to_models_registry()[odoo_module] += [model_type]
            concrete_models.append(model_type)

            # now we init these models properly
            # a bit like odoo.modules.loading#load_module_graph would do
            model = _build_and_register_model(model_type, self.env.registry, self.env.cr)

            _finish_model_setup(self.env, name)

            access_fields = [
                "id",
                "name",
                "model_id/id",
                "group_id/id",
                "perm_read",
                "perm_write",
                "perm_create",
                "perm_unlink",
            ]
            model._auto_fill_access_data(self.env, odoo_module, access_data)

        self.env["ir.model.access"].load(access_fields, access_data)
        self.env.registry.init_models(
            self.env.cr, remaining_models, {"module": odoo_module}
        )

        # init_models just created ir.model.data records for the "MAGIC FIELDS"
        # of the remaining_models. If we let these fields, next Odoo update
        # will decide that these MAGIC FIELDS do not match the fields of the
        # abstract schema mixins and would take a long time to delete these records
        # and the fields. This is not what we want, so we just remove these records:
        imd_magic_field_names = []
        for model in remaining_models:
            for field in models.MAGIC_COLUMNS + ["display_name", "__last_update"]:
                imd_magic_field_names.append(
                    f"field_{model.replace('.', '_')}__{field}"
                )
        imd_recs = self.env["ir.model.data"].search(
            [("name", "in", imd_magic_field_names)]
        )
        with mute_logger("odoo.models"):
            imd_recs.unlink()

        # The concrete classes we built above are rebuilt from scratch every
        # time this hook runs, but Odoo's MetaModel registered them in
        # module_to_models, which persists across registry rebuilds. Leaving
        # them there would make the next Registry.new() -- triggered by any
        # module install/update -- rebuild these *stale* classes as extra bases
        # of their model; being subclasses of the downstream classes that extend
        # the model via _inherit, they break the C3 linearization and crash
        # setup_models() with an inconsistent MRO (#4668). Drop exactly the ones
        # we just built; the hook recreates them on every registry (re)load.
        module_to_models = _module_to_models_registry()
        registered = module_to_models[odoo_module]
        module_to_models[odoo_module] = [
            cls for cls in registered if cls not in concrete_models
        ]

    @classmethod
    def _auto_fill_access_data(cls, env, module_name: str, access_data: list):
        """
        Fill access_data with a default user and a default manager access.
        """

        underline_name = cls._name.replace(".", "_")
        if module_name == "spec_driven_model":
            model_id = f"spec_driven_model.model_{underline_name}"
        else:
            model_id = f"{module_name}_spec.model_{underline_name}"
        user_access_name = f"access_{underline_name}_user"
        if not env["ir.model.access"].search(
            [
                ("name", "in", [underline_name, user_access_name]),
                ("model_id", "=", model_id),
            ]
        ):
            access_data.append(
                [
                    user_access_name,
                    user_access_name,
                    model_id,
                    f"{module_name}.group_user",
                    "1",
                    "0",
                    "0",
                    "0",
                ]
            )
        manager_access_name = f"access_{underline_name}_manager"
        if not env["ir.model.access"].search(
            [
                ("name", "in", [underline_name, manager_access_name]),
                ("model_id", "=", model_id),
            ]
        ):
            access_data.append(
                [
                    manager_access_name,
                    manager_access_name,
                    model_id,
                    f"{module_name}.group_manager",
                    "1",
                    "1",
                    "1",
                    "1",
                ]
            )
