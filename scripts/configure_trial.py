"""Prompt locally; never place the chosen code in Git history or CLI arguments."""
from getpass import getpass
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
code = getpass("设置四位数字体验码（输入不回显）: ").strip()
if len(code) != 4 or not code.isascii() or not code.isdigit():
    raise SystemExit("体验码必须是四位数字。")
if code != getpass("再次输入体验码: ").strip():
    raise SystemExit("两次输入不一致，未保存。")
lines = path.read_text().splitlines() if path.exists() else []
lines = [line for line in lines if not line.strip().startswith("TRIAL_CODES=")]
path.write_text("\n".join([*lines, "TRIAL_CODES=" + code]) + "\n")
path.chmod(0o600)
print("体验码已保存到 .env；重启服务后生效。不要提交或分享 .env。")
