# Copyright 2019-TODAY Akretion - Raphael Valyi <raphael.valyi@akretion.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.en.html).

import logging
import sys
from collections import OrderedDict, defaultdict
from importlib import import_module
from inspect import getmembers, isclass

from odoo import SUPERUSER_ID, _, api, models
from odoo.tools import mute_logger

from .ir_model import disambiguate_spec_labels

try:
    # Odoo 19+: _prepare_setup()/_setup_base() were turned from BaseModel
    # instance methods into module-level functions taking a model class
    # (env.registry[name]) instead of a recordset.
    from odoo.orm.model_classes import _prepare_setup as _odoo_prepare_setup
    from odoo.orm.model_classes import _setup as _odoo_setup_base
except ImportError:
    _odoo_prepare_setup = None
    _odoo_setup_base = None

SPEC_MIXIN_MAPPINGS = defaultdict(dict)  # by db

_logger = logging.getLogger(__name__)


def _prepare_and_setup_base(env, model_name):
    """
    This is required when you don't start odoo with -i (update) otherwise
    the model spec will not have its fields loaded yet.
    """
    if _odoo_prepare_setup is not None:  # Odoo 19+
        model_cls = env.registry[model_name]
        _odoo_prepare_setup(model_cls)
        _odoo_setup_base(model_cls, env)
    else:  # Odoo <= 18
        env[model_name]._prepare_setup()
        env[model_name]._setup_base()


def _field_args(field):
    """Odoo 19 renamed Field.args to Field._args__ (and made it read-only)."""
    return field._args__ if hasattr(field, "_args__") else field.args


def _set_field_args(field, **updates):
    """Update a field's stored constructor kwargs, across Odoo versions."""
    if hasattr(field, "_args__"):
        # _args__ is a ReadonlyDict on Odoo 19+: replace it wholesale.
        field._args__ = {**field._args__, **updates}
    else:
        field.args.update(updates)


def _mutate_spec_fields_comodel(cls, env):
    """
    Remap the comodel of relational fields pointing to spec mixins that were
    injected into (made concrete as) some other existing model, so they
    point to that concrete model instead of the abstract spec mixin.
    See SpecModel._setup_fields's docstring for why this logic lives here
    instead of directly in that method.
    """
    for klass in cls.__bases__:
        if not hasattr(klass, "_is_spec_driven"):
            continue
        if klass._name != cls._name:
            cls._map_concrete(env.cr.dbname, klass._name, cls._name)
            with mute_logger("odoo.tests.common"):
                klass._table = cls._table

    stacked_parents = [getattr(x, "_name", None) for x in cls.mro()]
    for name, field in cls._fields.items():
        if hasattr(field, "comodel_name") and field.comodel_name:
            comodel_name = field.comodel_name
            comodel = env[comodel_name]
            concrete_class = SPEC_MIXIN_MAPPINGS[env.cr.dbname].get(comodel._name)

            if (
                field.type == "many2one"
                and concrete_class is not None
                and comodel_name not in stacked_parents
            ):
                _logger.debug(
                    "    MUTATING m2o %s (%s) -> %s", name, comodel_name, concrete_class
                )
                field.original_comodel_name = comodel_name
                field.comodel_name = concrete_class

            elif field.type == "one2many":
                if concrete_class is not None:
                    _logger.debug(
                        "    MUTATING o2m %s (%s) -> %s",
                        name,
                        comodel_name,
                        concrete_class,
                    )
                    field.original_comodel_name = comodel_name
                    field.comodel_name = concrete_class
                if not hasattr(field, "inverse_name"):
                    continue
                inv_name = field.inverse_name
                for n, f in comodel._fields.items():
                    f_args = _field_args(f)
                    if n == inv_name and f_args and f_args.get("comodel_name"):
                        _logger.debug(
                            "    MUTATING m2o %s.%s (%s) -> %s",
                            comodel._name.split(".")[-1],
                            n,
                            f_args["comodel_name"],
                            cls._name,
                        )
                        _set_field_args(
                            f,
                            original_comodel_name=f_args["comodel_name"],
                            comodel_name=cls._name,
                        )


class SelectionMuteLogger(mute_logger):
    """
    The following fields.Selection warnings seem both very hard to
    avoid and benign in the spec_driven_model framework context.
    All in all, muting these 2 warnings seems like the best option.
    """

    def filter(self, record):
        msg = record.getMessage()
        if (
            "selection attribute will be ignored" in msg
            or "overrides existing selection" in msg
        ):
            return 0
        return super().filter(record)


class SpecModel(models.Model):
    """When you inherit this Model, then your model becomes concrete just like
    models.Model and it can use _inherit to inherit from several xsd generated
    spec mixins.
    All your model relational fields will be automatically mutated according to
    which concrete models the spec mixins where injected in.
    Because of this field mutation logic in _build_model, SpecModel should be
    inherited the Python way YourModel(spec_models.SpecModel)
    and not through _inherit.
    """

    _inherit = ["spec.mixin"]
    _auto = True  # automatically create database backend
    _register = False  # not visible in ORM registry
    _abstract = False
    _transient = False

    # TODO generic onchange method that check spec field simple type formats
    # xsd_required, according to the considered object context
    # and return warning or reformat things
    # ideally the list of onchange fields is set dynamically but if it is too
    # hard, we can just dump the list of fields when SpecModel is loaded

    # TODO a save python constraint that ensuire xsd_required fields for the
    # context are present

    @api.depends(lambda self: (self._rec_name,) if self._rec_name else ())
    def _compute_display_name(self):
        "More user friendly when automatic _rec_name is bad"
        res = super()._compute_display_name()
        for rec in self:
            if rec.display_name == "False" or not rec.display_name:
                rec.display_name = _("Open...")
        return res

    @classmethod
    def _build_model(cls, pool, cr):
        """
        xsd generated spec mixins do not need to depend on this opinionated
        module. That's why the spec.mixin is dynamically injected as a parent
        class as long as the generated spec mixins inherit from some
        spec.mixin.<schema_name> mixin.
        """
        cls._spec_pre_build(pool, cr)
        return super()._build_model(pool, cr)

    @classmethod
    def _spec_pre_build(cls, pool, cr):
        """
        Mutate this class's `_inherit` and the schema-wide mixin/mapping
        tables before Odoo turns the class into a concrete registry model.

        Odoo <= 18 called this via the per-model `_build_model(pool, cr)`
        classmethod hook, invoked from `Registry.load()` for every model
        definition. Odoo 19 dropped that hook: `Registry.load()` now calls
        the module-level `odoo.orm.model_classes.add_to_registry()`
        function directly instead of `model_def._build_model(pool, cr)`.
        On 19+ this method is instead invoked from the `add_to_registry`
        monkeypatch below, applied when this module loads.
        """
        # In Odoo 18+, the test framework monitors model attribute modifications
        # and logs stack traces. We suppress these during dynamic model building.
        with mute_logger("odoo.tests.common"):
            if hasattr(cls, "_spec_schema"):  # when called via _register_hook
                schema = cls._spec_schema
            else:
                mod = import_module(".".join(cls.__module__.split(".")[:-1]))
                schema = mod.spec_schema

            if schema and "spec.mixin" not in [
                c._name for c in pool[f"spec.mixin.{schema}"].__bases__
            ]:
                spec_mixin = pool[f"spec.mixin.{schema}"]
                spec_mixin._inherit = list(spec_mixin._inherit) + ["spec.mixin"]
                # Odoo 19 renamed the mangled `_BaseModel__base_classes`
                # attribute to `_base_classes__`.
                base_classes_attr = (
                    "_base_classes__"
                    if hasattr(spec_mixin, "_base_classes__")
                    else "_BaseModel__base_classes"
                )
                setattr(
                    spec_mixin,
                    base_classes_attr,
                    (pool["spec.mixin"],) + getattr(spec_mixin, base_classes_attr),
                )
                spec_mixin.__bases__ = (pool["spec.mixin"],) + spec_mixin.__bases__

            parents = [
                item[0] if isinstance(item, list) else item
                for item in list(cls._inherit)
            ]
            for parent in parents:
                # this will register that the spec mixins where injected in this class
                cls._map_concrete(cr.dbname, parent, cls._name)

    @api.model
    def _setup_base(self):
        with SelectionMuteLogger("odoo.fields"):  # mute spurious warnings
            return super()._setup_base()

    @api.model
    def _setup_fields(self):
        """
        SpecModel models inherit their fields from XSD generated mixins.
        These mixins can either be made concrete, either be injected into
        existing concrete Odoo models. In that last case, the comodels of the
        relational fields pointing to such mixins should be remapped to the
        proper concrete models where these mixins are injected.

        Odoo <= 18 called this instance method directly as part of the
        model setup pipeline, which is why the mutation logic below lives
        in a plain function instead (_mutate_spec_fields_comodel): Odoo 19's
        setup_model_classes() calls the module-level
        odoo.orm.model_classes._setup_fields(model_cls, env) function
        instead of this instance method, so on 19+ the same mutation is
        triggered from a monkeypatch of that function below.
        """
        _mutate_spec_fields_comodel(type(self), self.env)
        res = super()._setup_fields()
        disambiguate_spec_labels(type(self))
        return res

    @classmethod
    def _map_concrete(cls, dbname, key, target, quiet=False):
        if not quiet:
            _logger.debug(f"{key} ---> {target}")
        global SPEC_MIXIN_MAPPINGS
        SPEC_MIXIN_MAPPINGS[dbname][key] = target

    @classmethod
    def spec_module_classes(cls, spec_module):
        """
        Cache the list of spec_module classes to save calls to
        slow reflection API.
        """
        spec_module_attr = f"_spec_cache_{spec_module.replace('.', '_')}"
        if not hasattr(cls, spec_module_attr):
            # In Odoo 18+, the test framework monitors model attribute modifications
            # and logs stack traces. We suppress these during dynamic model building.
            with mute_logger("odoo.tests.common"):
                setattr(
                    cls, spec_module_attr, getmembers(sys.modules[spec_module], isclass)
                )
        return getattr(cls, spec_module_attr)

    @classmethod
    def _odoo_name_to_class(cls, odoo_name, spec_module):
        for _name, base_class in cls.spec_module_classes(spec_module):
            if base_class._name == odoo_name:
                return base_class
        return None


class StackedModel(SpecModel):
    """
    XML structures are typically deeply nested as this helps xsd
    validation. However, deeply nested objects in Odoo suck because that would
    mean crazy joins accross many tables and also an endless cascade of form
    popups.

    By inheriting from StackModel instead, your models.Model can
    instead inherit all the mixins that would correspond to the nested xsd
    nodes starting from the stacking_mixin. stacking_skip_paths allows you to avoid
    stacking specific nodes while stacking_force_paths will stack many2one
    entities even if they are not required.

    In Brazil it allows us to have mostly the fiscal
    document objects and the fiscal document line object with many details
    stacked in a denormalized way inside these two tables only.
    Because StackedModel has its _build_method overriden to do some magic
    during module loading it should be inherited the Python way
    with MyModel(spec_models.StackedModel).
    """

    _register = False  # forces you to inherit StackeModel properly

    @classmethod
    def _build_model(cls, pool, cr):
        cls._spec_pre_build(pool, cr)
        return super()._build_model(pool, cr)

    @classmethod
    def _spec_pre_build(cls, pool, cr):
        # see SpecModel._spec_pre_build for why this classmethod exists
        # instead of doing this work in _build_model directly.
        super()._spec_pre_build(pool, cr)
        # In Odoo 18+, the test framework monitors model attribute modifications
        # and logs stack traces. We suppress these during dynamic model building.
        with mute_logger("odoo.tests.common"):
            if hasattr(cls, "_spec_schema"):  # when called via _register_hook
                schema = cls._spec_schema
                version = cls._spec_version.replace(".", "")[:2]
            else:
                mod = import_module(".".join(cls.__module__.split(".")[:-1]))
                schema = mod.spec_schema
                version = mod.spec_version.replace(".", "")[:2]
            spec_prefix = f"{schema}{version}"
            setattr(cls, f"_{spec_prefix}_stacking_points", {})
        stacking_settings = {
            "odoo_module": getattr(cls, f"_{spec_prefix}_odoo_module"),  # TODO inherit?
            "stacking_mixin": getattr(cls, f"_{spec_prefix}_stacking_mixin"),
            "stacking_points": getattr(cls, f"_{spec_prefix}_stacking_points"),
            "stacking_skip_paths": getattr(
                cls, f"_{spec_prefix}_stacking_skip_paths", []
            ),
            "stacking_force_paths": getattr(
                cls, f"_{spec_prefix}_stacking_force_paths", []
            ),
        }
        # inject all stacked m2o as inherited classes
        _logger.info(f"building StackedModel {cls._name} {cls}")
        node = cls._odoo_name_to_class(
            stacking_settings["stacking_mixin"], stacking_settings["odoo_module"]
        )
        env = api.Environment(cr, SUPERUSER_ID, {})
        for kind, klass, _path, _field_path, _child_concrete in cls._visit_stack(
            env, node, stacking_settings
        ):
            if kind == "stacked" and klass not in cls.__bases__:
                cls._inherit.append(klass._name)

    @api.model
    def _add_field(self, name, field):
        """
        Overriden to avoid adding many2one fields that are in fact "stacking points"
        """
        if field.type == "many2one":
            for cls in type(self).mro():
                if issubclass(cls, StackedModel):
                    for attr in dir(cls):
                        if attr != "_get_stacking_points" and attr.endswith(
                            "_stacking_points"
                        ):
                            if name in getattr(cls, attr).keys():
                                # TODO it seems Odoo would still generate ir.model.data
                                # records for these fields we skip. They are deleted
                                # in IrModelData#_process_end. Eventually we could
                                # avoid creating these records or delete them.
                                return
        return super()._add_field(name, field)

    @classmethod
    def _visit_stack(cls, env, node, stacking_settings, path=None):
        """Pre-order traversal of the stacked models tree.
        1. This method is used to dynamically inherit all the spec models
        stacked together from an XML hierarchy.
        2. It is also useful to generate an automatic view of the spec fields.
        3. Finally it is used when exporting as XML.
        """
        if path is None:
            path = stacking_settings["stacking_mixin"].split(".")[-1]
        cls._map_concrete(env.cr.dbname, node._name, cls._name, quiet=True)
        yield "stacked", node, path, None, None

        fields = OrderedDict()
        # this is required when you don't start odoo with -i (update)
        # otherwise the model spec will not have its fields loaded yet.
        # TODO we may pass this env further instead of re-creating it.
        # TODO move setup_base just before the _visit_stack next call
        if node._name != cls._name or len(env[node._name]._fields.items() == 0):
            _prepare_and_setup_base(env, node._name)

        field_items = [(k, f) for k, f in env[node._name]._fields.items()]
        for i in field_items:
            fields[i[0]] = {
                "type": i[1].type,
                # TODO get with a function (lambda?)
                "comodel_name": i[1].comodel_name,
                "xsd_required": hasattr(i[1], "xsd_required") and i[1].xsd_required,
                "xsd_choice_required": hasattr(i[1], "xsd_choice_required")
                and i[1].xsd_choice_required,
            }
        for name, f in fields.items():
            if f["type"] not in [
                "many2one",
                "one2many",
            ] or name in stacking_settings.get("stacking_skip_paths", ""):
                # TODO change for view or export
                continue
            child = cls._odoo_name_to_class(
                f["comodel_name"], stacking_settings["odoo_module"]
            )
            if child is None:  # Not a spec field
                continue
            child_concrete = SPEC_MIXIN_MAPPINGS[env.cr.dbname].get(child._name)
            field_path = name.split("_")[1]  # remove schema prefix

            if f["type"] == "one2many":
                yield "one2many", node, path, field_path, child_concrete
                continue

            force_stacked = any(
                stack_path in path + "." + field_path
                for stack_path in stacking_settings.get("stacking_force_paths", [])
            )

            # many2one
            if (child_concrete is None or child_concrete == cls._name) and (
                f["xsd_required"] or f["xsd_choice_required"] or force_stacked
            ):
                # then we will STACK the child in the current class
                # In Odoo 18+, the test framework monitors model attribute modifications
                # and logs stack traces. We suppress these during dynamic model building
                with mute_logger("odoo.tests.common"):
                    child._stack_path = path
                child_path = f"{path}.{field_path}"
                stacking_settings["stacking_points"][name] = env[
                    node._name
                ]._fields.get(name)
                yield from cls._visit_stack(env, child, stacking_settings, child_path)
            else:
                yield "many2one", node, path, field_path, child_concrete


try:
    # Odoo 19+ only: Registry.load() no longer calls the per-model
    # `_build_model(pool, cr)` classmethod hook that SpecModel/StackedModel
    # rely on (see SpecModel._spec_pre_build's docstring). It calls the
    # module-level odoo.orm.model_classes.add_to_registry() function
    # directly instead. We wrap it so spec-driven model definitions still
    # get their `_inherit` mutated before Odoo merges them into a concrete
    # registry class.
    from odoo.orm import model_classes as _odoo_model_classes

    _original_add_to_registry = _odoo_model_classes.add_to_registry

    def _add_to_registry_with_spec_pre_build(registry, model_def):
        if issubclass(model_def, SpecModel):
            cr = registry.cursor()
            try:
                model_def._spec_pre_build(registry, cr)
            finally:
                cr.close()
        return _original_add_to_registry(registry, model_def)

    _odoo_model_classes.add_to_registry = _add_to_registry_with_spec_pre_build

    # Odoo 19 also stopped calling BaseModel._setup_fields() as an instance
    # method (which is what SpecModel._setup_fields overrides above to
    # mutate spec-mixin field comodels once their concrete destination is
    # known). setup_model_classes() now calls this module-level function
    # directly instead, so wrap it the same way.
    _original_setup_fields = _odoo_model_classes._setup_fields

    def _setup_fields_with_spec_mutation(model_cls, env):
        if issubclass(model_cls, SpecModel):
            _mutate_spec_fields_comodel(model_cls, env)
        _original_setup_fields(model_cls, env)
        if issubclass(model_cls, SpecModel):
            disambiguate_spec_labels(model_cls)

    _odoo_model_classes._setup_fields = _setup_fields_with_spec_mutation
except ImportError:
    # Odoo <= 18: the _build_model(pool, cr) classmethod hook (still
    # defined above for compat) is called directly by Registry.load(),
    # so no patching is needed.
    pass
