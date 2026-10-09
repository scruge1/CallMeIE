"""Existing summary handler, pure fake reads. No app import or provider calls."""
import ast
import asyncio
import sys
import time
import unittest
from unittest.mock import patch
from test_usage_projection import ROOT, usage


def summarize(payments, snapshot=None):
    node=next(n for n in ast.parse((ROOT/'server.py').read_text(encoding='utf-8')).body
              if isinstance(n,ast.AsyncFunctionDef) and n.name=='admin_operations_summary')
    node.decorator_list=[]
    now=int(time.time())
    async def stripe(*args): return payments
    async def vapi(start): return snapshot if snapshot is not None else {'status':'ok','observed_at':now,'calls':[]}
    def db(): raise RuntimeError('synthetic database unavailable')
    namespace={'Query':lambda value:value,'check_admin':lambda token:None,
               '_stripe_get':stripe,'_vapi_calls_window':vapi,'get_db':db,
               '_usage_tenant_bindings':lambda:{'status':'unavailable','rows':[]},
               'JSONResponse':lambda value:value,'VAPI_RATE_PER_MIN_EUR':0.05,'TWILIO_SMS_INTL_RATE_EUR':0.075}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<summary>','exec'),namespace)
    with patch.dict(sys.modules,{'billing.usage':usage}):
        return asyncio.run(namespace['admin_operations_summary']('synthetic'))


class MoneyTests(unittest.TestCase):
    def payment(self, **changes):
        return {'status':'succeeded','amount':1000,'created':int(time.time()),'currency':'eur',**changes}
    def test_empty_valid_read_is_partial_not_complete(self):
        result=summarize({'data':[]});self.assertEqual(result['money']['status'],'partial')
        self.assertFalse(result['money']['complete_period']);self.assertFalse(result['money']['profit_verified'])
    def test_eur_revenue_fees_are_explicit_estimate(self):
        result=summarize({'data':[self.payment()]});self.assertEqual(result['today']['revenue_minor'],1000)
        self.assertEqual(result['today']['settled_fees_minor'],39)
        self.assertEqual(result['money']['payment_fees_basis'],'estimated_percentage_plus_fixed_fee')
    def test_foreign_or_missing_currency_not_silently_eur(self):
        for currency in ['usd',None,'']:
            result=summarize({'data':[self.payment(currency=currency)]})
            self.assertEqual(result['money']['status'],'unavailable');self.assertFalse(result['money']['payment_currency_valid'])
    def test_mixed_currency_blocks_combined_money_display(self):
        self.assertEqual(summarize({'data':[self.payment(),self.payment(currency='usd')]})['money']['status'],'unavailable')
    def test_malformed_rows_not_successful_empty_data(self):
        for payload in [{},[],{'data':{}},{'data':[None]},{'data':[self.payment(amount=True)]},{'data':[self.payment(created=None)]}]:
            self.assertEqual(summarize(payload)['money']['status'],'unavailable')
    def test_failed_call_read_not_zero_cost_display(self):
        self.assertEqual(summarize({'data':[]},{'status':'unavailable','calls':[]})['money']['status'],'unavailable')
    def test_uncertain_call_timing_not_complete_cost_display(self):
        snapshot={'status':'ok','observed_at':int(time.time()),'calls':[{'id':'synthetic','status':'ended'}]}
        self.assertEqual(summarize({'data':[]},snapshot)['money']['status'],'unavailable')
    def test_payment_page_limit_explicit(self):
        for payload in [{'data':[],'has_more':True},{'data':[self.payment()]*100}]:
            self.assertTrue(summarize(payload)['money']['payment_limit_reached'])


if __name__=='__main__': unittest.main()
