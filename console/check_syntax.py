import traceback

try:
    with open('app.py', encoding='utf-8') as f:
        compile(f.read(), 'app.py', 'exec')
    print("Syntax OK")
except SyntaxError as e:
    print("SyntaxError found:")
    traceback.print_exc()
except Exception as e:
    print("Other error found:")
    traceback.print_exc()
