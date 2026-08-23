# Copyright (C) 2025 - TODAY Raphaël Valyi - Akretion
# License AGPL-3 - See http://www.gnu.org/licenses/agpl-3.0.html

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class FiscalDecoratorMixin(models.AbstractModel):
    _name = "l10n_br_account.decorator.mixin"
    _description = """A mixin to decorate l10n_br_fiscal_document(.line) easily.
    It specially deals with related and compute fields inherited with _inherits.
    """
    _fiscal_decorator_model = None

    # NOTE: on Odoo <= 18 the fiscal_document(_line)_id delegate field was left
    # non-required on purpose (some account.move have no fiscal document), and
    # this mixin used to override _inherits_check() to unset the required=True
    # the ORM auto-assigned. Odoo 19 hard-enforces delegate=True, required=True
    # and ondelete in ('cascade', 'restrict') on _inherits fields at model-class
    # setup time (odoo.orm.model_classes._check_inherits), raising a TypeError
    # that can't be worked around after the fact. fiscal_document(_line)_id is
    # now declared required=True directly on the field: Odoo's own _inherits
    # create() logic transparently auto-creates an (empty, cheap - neither
    # l10n_br_fiscal.document nor .document.line has any other required field)
    # fiscal document for every account.move(.line) that doesn't explicitly
    # supply one, so callers still don't need to pass fiscal data. The real
    # "is this actually a Brazilian fiscal document" signal used throughout
    # this module was always document_type_id being set, never whether
    # fiscal_document_id merely existed, so this doesn't change behavior.

    @api.model_create_multi
    def create(self, vals_list):
        return super(
            FiscalDecoratorMixin,
            self.with_context(create_from_account=True, allow_fiscal_access=True),
        ).create(vals_list)
