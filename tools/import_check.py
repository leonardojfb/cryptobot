import compileall
import ast
import os
import sys
import traceback
from importlib import util

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
EXCLUDE = ['.venv', '__pycache__']

print('Root:', ROOT)
# ensure project root is on sys.path so sibling imports resolve
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

print('\n1) Running compileall for syntax check...')
ok = compileall.compile_dir(ROOT, force=True, quiet=1)
print('compileall result:', ok)

failures = []

print('\n2) AST parsing each .py file (skipping venv and __pycache__)')
for dirpath, dirnames, filenames in os.walk(ROOT):
    # skip excluded directories
    if any(part in EXCLUDE for part in dirpath.split(os.sep)):
        continue
    for fn in filenames:
        if not fn.endswith('.py'):
            continue
        fp = os.path.join(dirpath, fn)
        rel = os.path.relpath(fp, ROOT)
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                src = f.read()
            ast.parse(src, filename=fp)
        except Exception as e:
            failures.append((rel, 'AST_PARSE', repr(e)))

print('AST parse failures:', len(failures))
for f in failures[:10]:
    print(' -', f[0], f[1], f[2])


print('\n3) Attempting to execute module top-level code to detect import/runtime errors')
mod_errors = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    if any(part in EXCLUDE for part in dirpath.split(os.sep)):
        continue
    for fn in filenames:
        if not fn.endswith('.py'):
            continue
        fp = os.path.join(dirpath, fn)
        rel = os.path.relpath(fp, ROOT)
        # skip the import_check itself
        if rel.startswith('tools' + os.sep) and fn == 'import_check.py':
            continue
        try:
            spec = util.spec_from_file_location(rel.replace(os.sep, '.'), fp)
            mod = util.module_from_spec(spec)
            # execute module top-level
            spec.loader.exec_module(mod)
        except Exception as e:
            tb = traceback.format_exc()
            mod_errors.append((rel, tb.splitlines()[-1], tb))

print('Module import/execution failures:', len(mod_errors))
for rel, msg, tb in mod_errors:
    print('\n---', rel, '---')
    print(msg)
    print(tb)

print('\nDone.')
