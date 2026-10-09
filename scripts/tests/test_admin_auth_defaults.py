"""Actual auth helper and environment default; no app startup or credentials."""
import ast
import hmac
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'server.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
DEFAULT = next(node for node in TREE.body if isinstance(node, ast.Assign)
               and any(isinstance(target,ast.Name) and target.id=='ADMIN_TOKEN' for target in node.targets))
GUARD = next(node for node in TREE.body if isinstance(node,ast.FunctionDef) and node.name=='check_admin')

class Unauthorized(Exception):
    def __init__(self,status_code,detail): self.status_code,self.detail=status_code,detail

def check(configured,supplied):
    ns={'ADMIN_TOKEN':configured,'hmac':hmac,'Query':lambda default:default,'HTTPException':Unauthorized}
    exec(compile(ast.Module(body=[GUARD],type_ignores=[]),'<admin-auth>','exec'),ns)
    return ns['check_admin'](supplied)

class AdminAuthTests(unittest.TestCase):
    def rejects(self,configured,supplied):
        with self.assertRaises(Unauthorized) as error: check(configured,supplied)
        self.assertEqual(error.exception.status_code,401)
        self.assertEqual(error.exception.detail,'Unauthorized')
    def test_missing_environment_has_no_known_default(self):
        ns={'os':SimpleNamespace(environ={})}
        exec(compile(ast.Module(body=[DEFAULT],type_ignores=[]),'<admin-default>', 'exec'),ns)
        self.assertEqual(ns['ADMIN_TOKEN'],'')
        self.rejects(ns['ADMIN_TOKEN'],'changeme')
    def test_known_placeholder_refuses_even_exact_match(self):
        for value in ('changeme','CHANGEME',' changeme '): self.rejects(value,value)
    def test_missing_and_wrong_tokens_refuse(self):
        for value in ('',None,{},'wrong'): self.rejects('synthetic-secret',value)
    def test_valid_existing_token_still_works(self): check('synthetic-secret','synthetic-secret')
    def test_unicode_existing_token_still_works(self): check('synthetic-é','synthetic-é')
    def test_no_trim_or_casefold_of_valid_credentials(self):
        self.rejects('synthetic-secret','SYNTHETIC-SECRET')
        self.rejects('synthetic-secret',' synthetic-secret ')

if __name__=='__main__': unittest.main()
