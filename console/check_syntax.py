import traceback
from pathlib import Path

try:
    app_path = Path(__file__).resolve().with_name("app.py")
    with app_path.open(encoding="utf-8") as f:
        compile(f.read(), str(app_path), "exec")
    print("Syntax OK")
except SyntaxError as e:
    print("SyntaxError found:")
    traceback.print_exc()
except Exception as e:
    print("Other error found:")
    traceback.print_exc()
