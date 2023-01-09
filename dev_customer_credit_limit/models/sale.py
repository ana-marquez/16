# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2015 DevIntelle Consulting Service Pvt.Ltd (<http://www.devintellecs.com>).
#
#    For Module Support : devintelle@gmail.com  or Skype : devintelle
#
##############################################################################

from odoo.exceptions import UserError, RedirectWarning, ValidationError

import json
from datetime import timedelta
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.tools import float_compare, float_round

import logging
_logger = logging.getLogger(__name__)

class sale_order(models.Model):
    _inherit= 'sale.order'
    
    exceeded_amount = fields.Float('Monto Excedido')
    
    free_current_order = fields.Boolean('Liberar Pedido')

    # state = fields.Selection([
    #     ('draft', 'Cotización'),
    #     ('sent', 'Cotización Enviada'),
    #     ('credit_limit', 'Limite de Credito'),
    #     ('sale', 'Venta'),
    #     ('done', 'Bloqueado'),
    #     ('cancel', 'Cancelado'),
    #     ], string='Estado', readonly=True, copy=False, index=True, track_visibility='onchange', track_sequence=3, default='draft')
    
    state = fields.Selection(selection_add=[('credit_limit', 'Limite de Credito')], ondelete={'credit_limit': 'set null'}) 


    @api.constrains('partner_id')
    def _constraint_sale_orders_on_partner_hold(self):
        # restrict_create_sale_orders_on_partner_hold
        for rec in self:
            if rec.partner_id and rec.partner_id.credit_limit_on_hold:
                parameter_hold = int(self.env['ir.config_parameter'].get_param('restrict_create_sale_orders_on_partner_hold')) or 0
                if parameter_hold:
                    raise UserError("El cliente se encuentra bloqueado por el limite de Credito.")
        return True

    @api.onchange('partner_id')
    def _onchange_partner_id_warning(self):
        super(sale_order,self)._onchange_partner_id_warning()
        partner_id = self.partner_id
        if self.partner_id.parent_id:
            partner_id = self.partner_id.parent_id
            
        if partner_id:
            if partner_id.credit_limit_on_hold:
                msg = "Cliente '" + partner_id.name + "' ha excedido su límite de crédito."
                return {'warning':
                            {'title': 'Limite de Credito Excedido', 'message': msg
                             }
                        }
    
    def action_sale_ok(self):
        partner_id = self.partner_id
        if self.partner_id.parent_id:
            partner_id = self.partner_id.parent_id
        partner_ids = [partner_id.id]
        for partner in partner_id.child_ids:
            partner_ids.append(partner.id)
        if self.free_current_order:
            self.action_confirm()

        #### Revervamos las Cantidades con un Albaran #####
        parameter = int(self.env['ir.config_parameter'].get_param('state_credit_limit_create_picking')) or 0
        _logger.info("\n########### Creamos el Albaran (Reservar) en estado Limite de Credito: %s " % parameter)
        if parameter >= 1:
            for line in self.order_line:
                precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
                procurements = []
                line = line.with_company(line.company_id)
                if line.product_id.type not in ('consu','product'):
                    continue
                qty = line._get_qty_procurement()
                if float_compare(qty, line.product_uom_qty, precision_digits=precision) == 0:
                    continue

                group_id = line._get_procurement_group()
                if not group_id:
                    group_id = self.env['procurement.group'].create(line._prepare_procurement_group_vals())
                    line.order_id.procurement_group_id = group_id
                else:
                    # In case the procurement group is already created and the order was
                    # cancelled, we need to update certain values of the group.
                    updated_vals = {}
                    if group_id.partner_id != line.order_id.partner_shipping_id:
                        updated_vals.update({'partner_id': line.order_id.partner_shipping_id.id})
                    if group_id.move_type != line.order_id.picking_policy:
                        updated_vals.update({'move_type': line.order_id.picking_policy})
                    if updated_vals:
                        group_id.write(updated_vals)

                values = line._prepare_procurement_values(group_id=group_id)
                product_qty = line.product_uom_qty - qty

                line_uom = line.product_uom
                quant_uom = line.product_id.uom_id
                product_qty, procurement_uom = line_uom._adjust_uom_quantities(product_qty, quant_uom)
                procurements.append(self.env['procurement.group'].Procurement(
                    line.product_id, product_qty, procurement_uom,
                    line.order_id.partner_shipping_id.property_stock_customer,
                    line.name, line.order_id.name, line.order_id.company_id, values))
            if procurements:
                self.env['procurement.group'].run(procurements)

        if partner_id.check_credit:
            ########## FACTURAS VENCIDAS ######
            parameter = int(self.env['ir.config_parameter'].get_param('tolerance_max_invoice_due')) or 5
            account_move_obj = self.env['account.move']
            current_date = fields.Date.context_today(self)
            if self.partner_id.tolerance_max_invoice_due:
                parameter = self.partner_id.tolerance_max_invoice_due
            account_move_due_ids = account_move_obj.sudo().search([('partner_id', 'in', partner_ids),
                                                                   ('invoice_date_due','<',current_date),
                                                                   ('move_type','=','out_invoice'),
                                                                   ('payment_state','in',('not_paid','in_payment','partial'))])
            if account_move_due_ids:
                for invoice in account_move_due_ids.sudo():
                    difference_days = (current_date - invoice.invoice_date_due).days
                    if difference_days >= parameter:
                        # raise UserError("No se puede confirmar el Pedido debido a que tiene una factura vencida: %s" % invoice.name)
                        imd = self.env['ir.model.data']
                        draft_invoice_lines_amount = "{:.2f}".format(invoice.amount_total)
                        to_invoice_amount = "{:.2f}".format(self.amount_total)
                        vals_wiz={
                            'partner_id':partner_id.id,
                            'sale_orders':'Valor de la Venta : '+ str(to_invoice_amount),
                            'invoices':str(len(invoice))+' Valor de la Factura : '+ str(draft_invoice_lines_amount),
                            'current_sale':self.amount_total or 0.0,
                            'exceeded_amount':0.0,
                            'credit':partner_id.credit,
                            'credit_limit_on_hold':partner_id.credit_limit_on_hold,
                            'invoice_due_on_hold': True,
                            }
                        self.write({'state': 'credit_limit'})
                        wiz_id = self.env['customer.limit.wizard'].create(vals_wiz)
                        action_ref = imd._xmlid_to_res_model_res_id('dev_customer_credit_limit.action_customer_limit_wizard')
                        action = self.env[action_ref[0]].browse(action_ref[1])
                        form_view_id = imd._xmlid_to_res_id('dev_customer_credit_limit.view_customer_limit_wizard_form')
                        return {
                                'name': action.name,
                                'help': action.help,
                                'type': action.type,
                                'views': [(form_view_id, 'form')],
                                'view_id': form_view_id,
                                'target': action.target,
                                'context': action.context,
                                'res_model': action.res_model,
                                'res_id':wiz_id.id,
                            }
            ###################################
            domain = [
                ('order_id.partner_id', 'in', partner_ids),
                ('order_id.state', 'in', ['sale', 'credit_limit','done'])]
            order_lines = self.env['sale.order.line'].search(domain)
            
            order = []
            to_invoice_amount = 0.0
            for line in order_lines:
                not_invoiced = line.product_uom_qty - line.qty_invoiced
                price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                taxes = line.tax_id.compute_all(
                    price, line.order_id.currency_id,
                    not_invoiced,
                    product=line.product_id, partner=line.order_id.partner_id)
                if line.order_id.id not in order:
                    if line.order_id.invoice_ids:
                        for inv in line.order_id.invoice_ids:
                            if inv.state == 'draft':
                                order.append(line.order_id.id)
                                break
                    else:
                        order.append(line.order_id.id)
                    
                to_invoice_amount += taxes['total_included']
            
            domain = [
                ('move_id.partner_id', 'in', partner_ids),
                ('move_id.state', '=', 'draft'),
                ('sale_line_ids', '!=', False)]
            draft_invoice_lines = self.env['account.move.line'].search(domain)
            for line in draft_invoice_lines:
                price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                taxes = line.tax_ids.compute_all(
                    price, line.move_id.currency_id,
                    line.quantity,
                    product=line.product_id, partner=line.move_id.partner_id)
                to_invoice_amount += taxes['total_included']

            # We sum from all the invoices lines that are in draft and not linked
            # to a sale order
            domain = [
                ('move_id.partner_id', 'in', partner_ids),
                ('move_id.state', '=', 'draft'),
                ('sale_line_ids', '=', False)]
            draft_invoice_lines = self.env['account.move.line'].search(domain)
            draft_invoice_lines_amount = 0.0
            invoice=[]
            for line in draft_invoice_lines:
                price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                taxes = line.tax_ids.compute_all(
                    price, line.move_id.currency_id,
                    line.quantity,
                    product=line.product_id, partner=line.move_id.partner_id)
                draft_invoice_lines_amount += taxes['total_included']
                if line.move_id.id not in invoice:
                    invoice.append(line.move_id.id)

            draft_invoice_lines_amount = "{:.2f}".format(draft_invoice_lines_amount)
            to_invoice_amount = "{:.2f}".format(to_invoice_amount)
            draft_invoice_lines_amount = float(draft_invoice_lines_amount)
            to_invoice_amount = float(to_invoice_amount)
            available_credit = partner_id.credit_limit - partner_id.credit - to_invoice_amount - draft_invoice_lines_amount

            if self.amount_total > available_credit:
                imd = self.env['ir.model.data']
                exceeded_amount = (to_invoice_amount + draft_invoice_lines_amount + partner_id.credit + self.amount_total) - partner_id.credit_limit
                exceeded_amount = "{:.2f}".format(exceeded_amount)
                exceeded_amount = float(exceeded_amount)
                vals_wiz={
                    'partner_id':partner_id.id,
                    'sale_orders':str(len(order))+ ' Valor de la Venta : '+ str(to_invoice_amount),
                    'invoices':str(len(invoice))+' Valor de la Factura : '+ str(draft_invoice_lines_amount),
                    'current_sale':self.amount_total or 0.0,
                    'exceeded_amount':exceeded_amount,
                    'credit':partner_id.credit,
                    'credit_limit_on_hold':partner_id.credit_limit_on_hold,
                    }
                self.write({'state': 'credit_limit'})
                wiz_id = self.env['customer.limit.wizard'].create(vals_wiz)
                action_ref = imd._xmlid_to_res_model_res_id('dev_customer_credit_limit.action_customer_limit_wizard')
                action = self.env[action_ref[0]].browse(action_ref[1])
                form_view_id = imd._xmlid_to_res_id('dev_customer_credit_limit.view_customer_limit_wizard_form')
                return {
                        'name': action.name,
                        'help': action.help,
                        'type': action.type,
                        'views': [(form_view_id, 'form')],
                        'view_id': form_view_id,
                        'target': action.target,
                        'context': action.context,
                        'res_model': action.res_model,
                        'res_id':wiz_id.id,
                    }
            else:
                self.action_confirm()
        else:
            self.action_confirm()
        return True
        
        
    def _make_url(self,model='sale.order'):
        base_url = self.env['ir.config_parameter'].get_param('web.base.url', default='http://localhost:8069')
        if base_url:
            base_url += '/web/login?db=%s&login=%s&key=%s#id=%s&model=%s' % (self._cr.dbname, '', '', self.id, model)
        return base_url

    def send_mail_approve_credit_limit(self): 
        _logger.info("\n######## Ya no envia el Correo Electronico >>>>>>>>>>>")
        return True


    # def send_mail_approve_credit_limit(self): 
    #     manager_group_id = self.env['ir.model.data'].get_object_reference('sales_team', 'group_sale_manager')[1]
    #     browse_group = self.env['res.groups'].browse(manager_group_id) 
    #     partner_id = self.partner_id
    #     if self.partner_id.parent_id:
    #         partner_id = self.partner_id.parent_id
        
    #     url = self._make_url('sale.order')
    #     subject = self.name + '-' + 'Requiere Aprobación de Credito'
    #     for user in browse_group.users:
    #         partner = user.partner_id
    #         body = '''
    #                     <b>Estimado cliente ''' " %s</b>," % (partner.name) + '''
    #                     <p> Su orden de venta ''' "<b><i>%s</i></b>" % self.name + '''  de ''' "<b><i>%s</i></b>" % partner_id.name +''' requiere la aprobación de nuestro departamenteo de ventas.</p> 
    #                     <p>Puede acceder a la orden de venta desde la siguiente URL <br/>
    #                     ''' "%s" % url +''' </p> 
                        
    #                     <p><b>Gracias por su preferencia,</b> <br/>
    #                     ''' "<b><i>%s</i></b>" % self.user_id.name +''' </p> 
    #                     ''' 
            
    #         mail_values = {
    #                     'email_from': self.user_id.email,
    #                     'email_to': partner.email,
    #                     'subject': subject,
    #                     'body_html': body,
    #                     'state': 'outgoing',
    #                 }
    #         mail_id =self.env['mail.mail'].create(mail_values)
    #         mail_id.send(True)


class AccountMove(models.Model):
    _name = 'account.move'
    _inherit ='account.move'

    exceeded_amount = fields.Float('Monto Excedido')

    free_current_order = fields.Boolean('Liberar Pedido')


    def action_post(self):
        for rec in self:
            partner_id = rec.partner_id
            if rec.partner_id.parent_id:
                partner_id = rec.partner_id.parent_id
            partner_ids = [partner_id.id]
            for partner in partner_id.child_ids:
                partner_ids.append(partner.id)
            if rec.free_current_order:
                result = super(AccountMove, self).action_post()
                return result
            if partner_id.check_credit:
                domain = [
                    ('order_id.partner_id', 'in', partner_ids),
                    ('order_id.state', 'in', ['sale', 'credit_limit','done'])]
                order_lines = self.env['sale.order.line'].search(domain)
                
                order = []
                to_invoice_amount = 0.0
                for line in order_lines:
                    not_invoiced = line.product_uom_qty - line.qty_invoiced
                    price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                    taxes = line.tax_id.compute_all(
                        price, line.order_id.currency_id,
                        not_invoiced,
                        product=line.product_id, partner=line.order_id.partner_id)
                    if line.order_id.id not in order:
                        if line.order_id.invoice_ids:
                            for inv in line.order_id.invoice_ids:
                                if inv.state == 'draft':
                                    order.append(line.order_id.id)
                                    break
                        else:
                            order.append(line.order_id.id)
                        
                    to_invoice_amount += taxes['total_included']
                
                domain = [
                    ('move_id.partner_id', 'in', partner_ids),
                    ('move_id.state', '=', 'draft'),
                    ('sale_line_ids', '!=', False)]
                draft_invoice_lines = self.env['account.move.line'].search(domain)
                for line in draft_invoice_lines:
                    price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                    taxes = line.tax_ids.compute_all(
                        price, line.move_id.currency_id,
                        line.quantity,
                        product=line.product_id, partner=line.move_id.partner_id)
                    to_invoice_amount += taxes['total_included']

                # We sum from all the invoices lines that are in draft and not linked
                # to a sale order
                domain = [
                    ('move_id.partner_id', 'in', partner_ids),
                    ('move_id.state', '=', 'draft'),
                    ('sale_line_ids', '=', False)]
                draft_invoice_lines = self.env['account.move.line'].search(domain)
                draft_invoice_lines_amount = 0.0
                invoice=[]
                for line in draft_invoice_lines:
                    price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                    taxes = line.tax_ids.compute_all(
                        price, line.move_id.currency_id,
                        line.quantity,
                        product=line.product_id, partner=line.move_id.partner_id)
                    draft_invoice_lines_amount += taxes['total_included']
                    if line.move_id.id not in invoice:
                        invoice.append(line.move_id.id)

                draft_invoice_lines_amount = "{:.2f}".format(draft_invoice_lines_amount)
                to_invoice_amount = "{:.2f}".format(to_invoice_amount)
                draft_invoice_lines_amount = float(draft_invoice_lines_amount)
                to_invoice_amount = float(to_invoice_amount)
                available_credit = partner_id.credit_limit - partner_id.credit - to_invoice_amount - draft_invoice_lines_amount

                if self.amount_total > available_credit:
                    imd = self.env['ir.model.data']
                    exceeded_amount = (to_invoice_amount + draft_invoice_lines_amount + partner_id.credit + self.amount_total) - partner_id.credit_limit
                    exceeded_amount = "{:.2f}".format(exceeded_amount)
                    exceeded_amount = float(exceeded_amount)
                    vals_wiz={
                        'partner_id':partner_id.id,
                        'sale_orders':str(len(order))+ ' Valor de la Venta : '+ str(to_invoice_amount),
                        'invoices':str(len(invoice))+' Valor de la Factura : '+ str(draft_invoice_lines_amount),
                        'current_sale':self.amount_total or 0.0,
                        'exceeded_amount':exceeded_amount,
                        'credit':partner_id.credit,
                        'credit_limit_on_hold':partner_id.credit_limit_on_hold,
                        }
                    wiz_id = self.env['customer.limit.wizard'].create(vals_wiz)
                    action_ref = imd._xmlid_to_res_model_res_id('dev_customer_credit_limit.action_customer_limit_wizard')
                    action = self.env[action_ref[0]].browse(action_ref[1])
                    form_view_id = imd._xmlid_to_res_id('dev_customer_credit_limit.view_customer_limit_wizard_form')
                    return {
                            'name': action.name,
                            'help': action.help,
                            'type': action.type,
                            'views': [(form_view_id, 'form')],
                            'view_id': form_view_id,
                            'target': action.target,
                            'context': action.context,
                            'res_model': action.res_model,
                            'res_id':wiz_id.id,
                        }

        result = super(AccountMove, self).action_post()
        return result

    def send_mail_approve_credit_limit(self): 
        _logger.info("\n######## Ya no envia el Correo Electronico >>>>>>>>>>>")
        return True


    # def send_mail_approve_credit_limit(self): 
    #     manager_group_id = self.env['ir.model.data'].get_object_reference('sales_team', 'group_sale_manager')[1]
    #     browse_group = self.env['res.groups'].browse(manager_group_id) 
    #     partner_id = self.partner_id
    #     if self.partner_id.parent_id:
    #         partner_id = self.partner_id.parent_id
        
    #     url = self._make_url('account.move')
    #     subject = self.name + '-' + 'Requiere Aprobación de Credito'
    #     for user in browse_group.users:
    #         partner = user.partner_id
    #         body = '''
    #                     <b>Estimado cliente ''' " %s</b>," % (partner.name) + '''
    #                     <p> Su factura ''' "<b><i>%s</i></b>" % self.name + '''  de ''' "<b><i>%s</i></b>" % partner_id.name +''' requiere la aprobación de nuestro departamenteo de ventas.</p> 
    #                     <p>Puede acceder a la factura desde la siguiente URL <br/>
    #                     ''' "%s" % url +''' </p> 
                        
    #                     <p><b>Gracias por su preferencia,</b> <br/>
    #                     ''' "<b><i>%s</i></b>" % self.user_id.name +''' </p> 
    #                     ''' 
            
    #         mail_values = {
    #                     'email_from': self.user_id.email,
    #                     'email_to': partner.email,
    #                     'subject': subject,
    #                     'body_html': body,
    #                     'state': 'outgoing',
    #                 }
    #         mail_id =self.env['mail.mail'].create(mail_values)
    #         mail_id.send(True)


    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        super(AccountMove,self)._onchange_partner_id()
        partner_id = self.partner_id
        if self.partner_id.parent_id:
            partner_id = self.partner_id.parent_id
        if partner_id:
            if partner_id.credit_limit_on_hold:
                msg = "Cliente '" + partner_id.name + "' ha excedido su límite de crédito."
                return {'warning':
                            {'title': 'Limite de Credito Excedido', 'message': msg
                             }
                        }
