"""Проверка загруженного скрипта сцены Blender (Build_*.py) перед запуском на сервере рендера.
Скрипт приходит от пользователя, поэтому: разрешены только безопасные импорты, запрещены файлы/сеть/процессы/
eval/exec/dunder-доступ, а вредные для нашего мультфильма вызовы (удаление всех объектов, сохранение .blend,
собственный рендер) вырезаются. Без bpy — работает и в боте (при загрузке архива), и в Blender (перед запуском).
Это защита от случайностей и грубых атак, а не полноценная песочница: на VPS рендер идёт в Docker-контейнере."""
import ast

ALLOWED_IMPORTS = {"bpy", "bmesh", "math", "random", "mathutils", "colorsys", "itertools", "functools"}
BANNED_NAMES = {"eval", "exec", "compile", "open", "__import__", "input", "breakpoint", "exit", "quit",
                "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview", "help"}
# bpy.ops.<группа>.* целиком запрещены
BANNED_OPS = {"wm", "script", "preferences", "import_scene", "export_scene", "import_mesh", "export_mesh",
              "import_image", "image", "text", "file", "sequencer", "screen", "outliner", "render", "scene",
              "workspace", "ed", "info", "console", "nla", "sound", "clip", "gpencil", "paint"}
# bpy.ops.<группа>.<имя>(...) вызовы — молча пропускаем (они ломают общую сцену мультфильма)
SKIP_OPS = {("object", "delete"), ("object", "select_all"), ("object", "select_by_type"), ("outliner", "delete")}
BANNED_DATA = {"libraries", "texts", "filepath", "scripts"}


class SceneScriptError(ValueError):
    pass


def _chain(node) -> list[str]:
    """a.b.c(...) -> ["a", "b", "c"]"""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
    return out[::-1]


class _Sanitize(ast.NodeTransformer):
    def visit_Import(self, n):
        for a in n.names:
            if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                raise SceneScriptError(f"Запрещённый import: {a.name}")
        return n

    def visit_ImportFrom(self, n):
        if (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
            raise SceneScriptError(f"Запрещённый import: {n.module}")
        return n

    def visit_Name(self, n):
        if n.id in BANNED_NAMES:
            raise SceneScriptError(f"Запрещено использовать «{n.id}»")
        if n.id.startswith("__") and n.id.endswith("__") and n.id != "__name__":
            raise SceneScriptError(f"Запрещено использовать «{n.id}»")
        return n

    def visit_Attribute(self, n):
        if n.attr.startswith("__") and n.attr.endswith("__"):
            raise SceneScriptError(f"Запрещён доступ к «{n.attr}»")
        ch = _chain(n)
        if ch[:2] == ["bpy", "data"] and len(ch) > 2 and ch[2] in BANNED_DATA:
            raise SceneScriptError(f"Запрещён доступ к bpy.data.{ch[2]}")
        if ch[:2] == ["bpy", "app"] and len(ch) > 2 and ch[2] not in ("version", "version_string"):
            raise SceneScriptError("Запрещён доступ к bpy.app")
        if ch[:2] == ["bpy", "utils"]:
            raise SceneScriptError("Запрещён доступ к bpy.utils")
        if ch[:2] == ["bpy", "ops"] and len(ch) > 2 and ch[2] in BANNED_OPS:
            return ast.copy_location(ast.Name(id="_blocked_op", ctx=ast.Load()), n)   # вызов станет безвредным
        self.generic_visit(n)
        return n

    def visit_Expr(self, n):
        if isinstance(n.value, ast.Call):
            ch = _chain(n.value.func)
            if ch[:2] == ["bpy", "ops"] and len(ch) >= 4 and (ch[2], ch[3]) in SKIP_OPS:
                return ast.copy_location(ast.Pass(), n)
            if ch[:2] == ["bpy", "ops"] and len(ch) >= 3 and ch[2] in BANNED_OPS:
                return ast.copy_location(ast.Pass(), n)
        self.generic_visit(n)
        return n


def sanitize(source: str, filename: str = "scene.py"):
    """Возвращает code-объект безопасной версии скрипта или бросает SceneScriptError."""
    if len(source) > 400_000:
        raise SceneScriptError("Скрипт сцены слишком большой")
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as e:
        raise SceneScriptError(f"Ошибка в скрипте сцены (строка {e.lineno}): {e.msg}")
    tree = _Sanitize().visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")


def check(source: str) -> None:
    sanitize(source)
