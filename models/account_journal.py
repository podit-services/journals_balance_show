
from odoo import _, models
from collections import defaultdict


def group_by_journal(vals_list):
    res = defaultdict(list)
    for vals in vals_list:
        res[vals['journal_id']].append(vals)
    return res

class AccountJournal(models.Model):
    _inherit = "account.journal"
    
    def _fill_sale_purchase_dashboard_data(self, dashboard_data):
        """Populate all sale and purchase journal's data dict with relevant information for the kanban card."""
        sale_purchase_journals = self.filtered(lambda journal: journal.type in ('sale', 'purchase'))
        if not sale_purchase_journals:
            return
        field_list = [
            "account_move.journal_id",
            "(CASE WHEN account_move.move_type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END) * account_move.amount_residual AS amount_total",
            "(CASE WHEN account_move.move_type IN ('in_invoice', 'in_refund', 'in_receipt') THEN -1 ELSE 1 END) * account_move.amount_residual_signed AS amount_total_company",
            "account_move.currency_id AS currency",
            "account_move.move_type",
            "account_move.invoice_date",
            "account_move.company_id",
        ]
        query, params = sale_purchase_journals._get_open_bills_to_pay_query().select(*field_list)
        self.env.cr.execute(query, params)
        query_results_to_pay = group_by_journal(self.env.cr.dictfetchall())

        query, params = sale_purchase_journals._get_draft_bills_query().select(*field_list)
        self.env.cr.execute(query, params)
        query_results_drafts = group_by_journal(self.env.cr.dictfetchall())

        query, params = sale_purchase_journals._get_late_bills_query().select(*field_list)
        self.env.cr.execute(query, params)
        late_query_results = group_by_journal(self.env.cr.dictfetchall())

        to_check_vals = {
            vals['journal_id'][0]: vals
            for vals in self.env['account.move'].read_group(
                domain=[('journal_id', 'in', sale_purchase_journals.ids), ('to_check', '=', True)],
                fields=['amount_total_signed'],
                groupby='journal_id',
            )
        }

        curr_cache = {}
        sale_purchase_journals._fill_dashboard_data_count(dashboard_data, 'account.move', 'entries_count', [])
        for journal in sale_purchase_journals:
            currency = journal.currency_id or journal.company_id.currency_id
            (number_waiting, sum_waiting) = super()._count_results_and_sum_amounts(query_results_to_pay[journal.id], currency, curr_cache=curr_cache)
            (number_draft, sum_draft) = super()._count_results_and_sum_amounts(query_results_drafts[journal.id], currency, curr_cache=curr_cache)
            (number_late, sum_late) = super()._count_results_and_sum_amounts(late_query_results[journal.id], currency, curr_cache=curr_cache)
            to_check = to_check_vals.get(journal.id, {})
            dashboard_data[journal.id].update({
                'number_to_check': to_check.get('journal_id_count', 0),
                'to_check_balance': currency.format(to_check.get('amount_total_signed', 0)),
                'title': _('Bills to pay') if journal.type == 'purchase' else _('Invoices owed to you'),
                'number_draft': number_draft,
                'number_waiting': number_waiting,
                'number_late': number_late,
                'sum_draft': currency.format(sum_draft),
                'sum_waiting': currency.format(sum_waiting),
                'sum_late': currency.format(sum_late),
                'has_sequence_holes': journal.has_sequence_holes,
                'is_sample_data': dashboard_data[journal.id]['entries_count'],
                'balance': currency.format(journal.default_account_id.current_balance),
                'show_balance':True,
            })
    
    def _fill_bank_cash_dashboard_data(self, dashboard_data):
        """Populate all bank and cash journal's data dict with relevant information for the kanban card."""
        bank_cash_journals = self.filtered(lambda journal: journal.type in ('bank', 'cash'))
        if not bank_cash_journals:
            return

        # Number to reconcile
        self._cr.execute("""
            SELECT st_line_move.journal_id,
                   COUNT(st_line.id)
              FROM account_bank_statement_line st_line
              JOIN account_move st_line_move ON st_line_move.id = st_line.move_id
             WHERE st_line_move.journal_id IN %s
               AND NOT st_line.is_reconciled
               AND st_line_move.to_check IS NOT TRUE
               AND st_line_move.state = 'posted'
          GROUP BY st_line_move.journal_id
        """, [tuple(bank_cash_journals.ids)])
        number_to_reconcile = {
            journal_id: count
            for journal_id, count in self.env.cr.fetchall()
        }

        # Last statement
        self.env.cr.execute("""
            SELECT journal.id, statement.id
              FROM account_journal journal
         LEFT JOIN LATERAL (
                      SELECT id
                        FROM account_bank_statement
                       WHERE journal_id = journal.id
                    ORDER BY first_line_index DESC
                       LIMIT 1
                   ) statement ON TRUE
             WHERE journal.id = ANY(%s)
        """, [self.ids])
        last_statements = {journal_id: statement_id for journal_id, statement_id in self.env.cr.fetchall()}
        self.env['account.bank.statement'].browse(i for i in last_statements.values() if i).mapped('balance_end_real')  # prefetch

        outstanding_pay_account_balances = bank_cash_journals._get_journal_dashboard_outstanding_payments()

        # To check
        to_check = {
            res['journal_id'][0]: (res['amount'], res['journal_id_count'])
            for res in self.env['account.bank.statement.line'].read_group(
                domain=[
                    ('journal_id', 'in', bank_cash_journals.ids),
                    ('move_id.to_check', '=', True),
                    ('move_id.state', '=', 'posted'),
                ],
                fields=['amount'],
                groupby=['journal_id'],
            )
        }

        for journal in bank_cash_journals:
            last_statement = self.env['account.bank.statement'].browse(last_statements.get(journal.id))
            currency = journal.currency_id or journal.company_id.currency_id
            has_outstanding, outstanding_pay_account_balance = outstanding_pay_account_balances[journal.id]
            to_check_balance, number_to_check = to_check.get(journal.id, (0, 0))

            dashboard_data[journal.id].update({
                'number_to_check': number_to_check,
                'to_check_balance': currency.format(to_check_balance),
                'number_to_reconcile': number_to_reconcile.get(journal.id, 0),
                'account_balance': currency.format(journal.current_statement_balance),
                'has_at_least_one_statement': bool(last_statement),
                'nb_lines_bank_account_balance': bool(journal.has_statement_lines),
                'outstanding_pay_account_balance': currency.format(outstanding_pay_account_balance),
                'nb_lines_outstanding_pay_account_balance': has_outstanding,
                'last_balance': currency.format(last_statement.balance_end_real),
                'bank_statements_source': journal.bank_statements_source,
                'is_sample_data': journal.has_statement_lines,
                'balance': currency.format(journal.default_account_id.current_balance),
                'show_balance':True,
            })
            
    def _fill_general_dashboard_data(self, dashboard_data):
        """Populate all miscelaneous journal's data dict with relevant information for the kanban card."""
        general_journals = self.filtered(lambda journal: journal.type == 'general')
        if not general_journals:
            return
        to_check_vals = {
            vals['journal_id'][0]: vals
            for vals in self.env['account.move'].read_group(
                domain=[('journal_id', 'in', general_journals.ids), ('to_check', '=', True)],
                fields=['amount_total_signed'],
                groupby='journal_id',
                lazy=False,
            )
        }
        for journal in general_journals:
            currency = journal.currency_id or journal.company_id.currency_id
            vals = to_check_vals.get(journal.id, {})
            dashboard_data[journal.id].update({
                'number_to_check': vals.get('__count', 0),
                'to_check_balance': currency.format(vals.get('amount_total_signed', 0)),
                'balance': currency.format(journal.default_account_id.current_balance),
                'show_balance':True,
            })                
    