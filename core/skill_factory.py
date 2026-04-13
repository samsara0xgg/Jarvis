"""Claude Code 技能工厂 — 调用 CC CLI 生成新 skill 文件。

流程：检查已有 → 清理残留 → 调 CC → 安全扫描 → pytest → 成功/全部清理
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

_DANGEROUS_PATTERNS = [
    (r"\bos\.system\b", "os.system"),
    (r"\bos\.popen\b", "os.popen"),
    (r"\bsubprocess\b", "subprocess module"),
    (r"\beval\s*\(", "eval()"),
    (r"\bexec\s*\(", "exec()"),
    (r"\b__import__\b", "__import__()"),
    (r"\bshutil\b", "shutil module"),
    (r"\bpickle\b", "pickle module"),
    (r"\bimportlib\b", "importlib module"),
    (r"\bctypes\b", "ctypes module"),
    (r"\bsocket\b", "socket module"),
    (r"\bcompile\s*\(", "compile()"),
    (r"\bglobals\s*\(", "globals()"),
    (r"\blocals\s*\(", "locals()"),
    (r"\bsetattr\s*\(", "setattr()"),
    (r"\bgetattr\s*\(.+(?:system|popen|exec|eval)", "getattr() with dangerous target"),
]


class SkillFactory:
    """Generate new skills by invoking Claude Code CLI.

    Args:
        learned_dir: Path to skills/learned/ directory.
        project_root: Path to the project root.
    """

    def __init__(
        self,
        learned_dir: str | Path = "skills/learned",
        project_root: str | Path = ".",
    ) -> None:
        self._dir = Path(learned_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._root = Path(project_root)
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        """Kill the running CC subprocess, if any."""
        proc = self._process
        if proc and proc.poll() is None:
            LOGGER.info("Killing SkillFactory CC subprocess (pid=%d)", proc.pid)
            proc.kill()

    def has_skill(self, skill_id: str) -> bool:
        """Check if a working skill file exists for this skill_id."""
        for f in self._dir.glob("*.py"):
            if f.stem == skill_id and f.name != "__init__.py":
                return True
        return False

    def create(
        self,
        description: str,
        skill_name_hint: str = "",
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Generate a new skill from a natural language description.

        Returns:
            {"success": bool, "skill_name": str, "message": str, "path": str | None}
        """
        def status(msg: str) -> None:
            LOGGER.info("SkillFactory: %s", msg)
            if on_status:
                on_status(msg)

        abc_source = self._read_file("skills/__init__.py")
        example_source = self._read_file("skills/weather.py")
        prompt = self._build_prompt(description, abc_source, example_source)
        skill_id = skill_name_hint or self._slugify(description)
        init_file = self._dir / "__init__.py"

        status(f"Prompt: {description[:80]}")

        # --- 清理所有残留文件（之前失败/超时留下的） ---
        self._cleanup_files(skill_id, init_file)

        # --- 记录 CC 调用前的文件快照 ---
        existing_mtimes = {
            f: f.stat().st_mtime
            for f in self._dir.glob("*.py") if f != init_file
        }

        # --- 调用 CC ---
        import shutil
        claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        status(f"调用 CC（skill_id={skill_id}, bin={claude_bin}）")

        try:
            self._process = subprocess.Popen(
                [claude_bin, "-p", prompt, "--allowedTools", "Edit,Write,Bash", "--output-format", "text"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(self._root),
            )

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def _stream(pipe: Any, lines: list[str], label: str) -> None:
                for line in pipe:
                    line = line.rstrip()
                    if line:
                        lines.append(line)
                        LOGGER.info("CC[%s]: %s", label, line)

            t_out = threading.Thread(target=_stream, args=(self._process.stdout, stdout_lines, "out"), daemon=True)
            t_err = threading.Thread(target=_stream, args=(self._process.stderr, stderr_lines, "err"), daemon=True)
            t_out.start()
            t_err.start()

            try:
                self._process.wait(timeout=180)
                returncode = self._process.returncode
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
                self._cleanup_new_files(existing_mtimes, init_file)
                return self._fail(skill_id, "Claude Code 超时（180s）")
            finally:
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                self._process = None

            status(f"CC 返回码: {returncode}")
            if returncode != 0:
                self._cleanup_new_files(existing_mtimes, init_file)
                stderr = "\n".join(stderr_lines)
                return self._fail(skill_id, f"CC 执行失败: {stderr[:200]}")

        except FileNotFoundError:
            self._process = None
            return self._fail(skill_id, "Claude Code CLI 未安装")

        # --- 扫描新增/修改的文件 ---
        changed_files: set[Path] = set()
        for f in self._dir.glob("*.py"):
            if f == init_file:
                continue
            if f not in existing_mtimes:
                changed_files.add(f)
            elif f.stat().st_mtime > existing_mtimes[f]:
                changed_files.add(f)

        if not changed_files:
            return self._fail(skill_id, "CC 未生成任何 .py 文件")

        skill_path = sorted(changed_files)[0]
        actual_skill_id = skill_path.stem
        status(f"生成文件: {skill_path.name}")

        # --- 安全扫描 ---
        status("安全检查...")
        security_errors = self._security_scan(str(skill_path))
        if security_errors:
            status(f"安全检查失败: {security_errors}")
            self._cleanup_new_files(existing_mtimes, init_file)
            return self._fail(actual_skill_id, f"安全检查未通过: {'; '.join(security_errors)}")
        status("安全检查通过")

        # --- 跑测试 ---
        test_path = self._root / "tests" / f"test_learned_{actual_skill_id}.py"
        alt_test_path = self._root / "tests" / f"test_{actual_skill_id}.py"
        found_test = test_path if test_path.exists() else (alt_test_path if alt_test_path.exists() else None)

        if found_test:
            status(f"运行测试: {found_test.name}")
            try:
                test_result = subprocess.run(
                    ["python", "-m", "pytest", str(found_test), "-v", "--tb=short"],
                    capture_output=True, text=True, timeout=30, cwd=str(self._root),
                )
                status(f"测试结果:\n{test_result.stdout[-400:]}")
                if test_result.returncode != 0:
                    self._cleanup_new_files(existing_mtimes, init_file)
                    return self._fail(actual_skill_id, f"测试未通过:\n{test_result.stdout[-300:]}")
            except subprocess.TimeoutExpired:
                self._cleanup_new_files(existing_mtimes, init_file)
                return self._fail(actual_skill_id, "测试执行超时")
        else:
            status("未找到测试文件，跳过测试")

        status("学会了！")
        return {"success": True, "skill_name": actual_skill_id, "message": "技能学习成功", "path": str(skill_path)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fail(self, skill_id: str, message: str) -> dict[str, Any]:
        """Return a standardized failure result."""
        LOGGER.warning("SkillFactory failed (%s): %s", skill_id, message)
        return {"success": False, "skill_name": skill_id, "message": message, "path": None}

    def _cleanup_files(self, skill_id: str, init_file: Path) -> None:
        """Remove stale files from previous failed attempts for this skill_id."""
        for old in self._dir.glob(f"{skill_id}*.py"):
            if old != init_file:
                LOGGER.info("Cleaning stale: %s", old.name)
                old.unlink(missing_ok=True)
        for old in (self._root / "tests").glob(f"test_learned_{skill_id}*.py"):
            LOGGER.info("Cleaning stale test: %s", old.name)
            old.unlink(missing_ok=True)
        # Also clean files CC might have named differently
        for old in self._dir.glob("*.py"):
            if old == init_file:
                continue
            # If file was created recently (within last 5 min) and is empty or tiny, remove
            try:
                if old.stat().st_size < 50:
                    LOGGER.info("Cleaning empty stub: %s", old.name)
                    old.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_new_files(self, before_mtimes: dict[Path, float], init_file: Path) -> None:
        """Remove ALL files created/modified since the snapshot. Called on any failure."""
        for f in self._dir.glob("*.py"):
            if f == init_file:
                continue
            if f not in before_mtimes:
                LOGGER.info("Cleanup (new file): %s", f.name)
                f.unlink(missing_ok=True)
            elif f.stat().st_mtime > before_mtimes[f]:
                LOGGER.info("Cleanup (modified): %s", f.name)
                f.unlink(missing_ok=True)
        # Clean test files too
        for f in (self._root / "tests").glob("test_learned_*.py"):
            try:
                if f not in before_mtimes and f.stat().st_mtime > min(before_mtimes.values(), default=0):
                    LOGGER.info("Cleanup test: %s", f.name)
                    f.unlink(missing_ok=True)
            except OSError:
                pass

    def _build_prompt(self, description: str, skill_abc_source: str, example_skill_source: str) -> str:
        return f"""你需要为 Jarvis 语音助手写一个新的 skill。

## 需求
{description}

## Skill 接口（必须继承 Skill）
```python
{skill_abc_source}
```

## 范例 skill（参考格式和风格）
```python
{example_skill_source}
```

## 要求
1. 在 skills/learned/ 目录下创建一个 .py 文件，文件名用英文下划线命名
2. 继承 Skill ABC，实现 skill_name、get_tool_definitions、execute 三个方法
3. __init__ 不要有必填参数（不要 config 参数），硬编码默认值即可
4. 在 tests/ 目录下创建对应的测试文件 test_learned_<name>.py
5. execute 方法接收 tool_name 和 tool_input，返回文本结果字符串
6. 网络请求用 requests 库，设置 timeout=10
7. 禁止使用 os.system、subprocess、eval、exec
8. 禁止读写 core/ 目录下的文件
9. 用 logging 模块，不用 print
10. 加 type hints
11. 只创建文件，不要输出其他说明文字"""

    def _security_scan(self, file_path: str) -> list[str]:
        content = Path(file_path).read_text()
        errors = []
        for pattern, desc in _DANGEROUS_PATTERNS:
            if re.search(pattern, content):
                errors.append(desc)
        return errors

    def _read_file(self, rel_path: str) -> str:
        path = self._root / rel_path
        if path.exists():
            return path.read_text()
        return f"# File not found: {rel_path}"

    def _slugify(self, text: str) -> str:
        """Convert a description to a valid Python module name."""
        import hashlib
        slug = text[:30].strip().lower()
        slug = re.sub(r"[^\w\s]", "", slug)
        slug = re.sub(r"\s+", "_", slug)
        slug = re.sub(r"[^\x00-\x7f]", "", slug)
        slug = slug.strip("_")
        if not slug:
            h = hashlib.md5(text.encode()).hexdigest()[:6]
            slug = f"skill_{h}"
        return slug
