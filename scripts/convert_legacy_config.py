"""Convert WZML-X config.env values into the upstream config.py format."""

import argparse
import ast
import json
from pathlib import Path

ALIASES = {
    "FSUB_IDS": "FORCE_SUB_IDS",
    "LEECH_LOG_ID": "LEECH_LOG_CHAT",
    "LEECH_FILENAME_CAPTION": "LEECH_CAPTION",
    "LEECH_FILENAME_PREFIX": "LEECH_PREFIX",
    "LEECH_FILENAME_SUFFIX": "LEECH_SUFFIX",
    "CAP_FONT": "LEECH_FONT",
    "STORAGE_THRESHOLD": "STORAGE_LIMIT",
    "INCOMPLETE_TASK_NOTIFIER": "INC_TASK_NOTIFY",
    "DISABLE_DRIVE_LINK": "SHOW_CLOUD_LINK",
}


def _safe_eval(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_safe_eval(item) for item in node.elts]
        return type({ast.List: list, ast.Tuple: tuple, ast.Set: set}[type(node)])(values)
    if isinstance(node, ast.Dict):
        return {_safe_eval(k): _safe_eval(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _safe_eval(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)):
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left // right
    raise ValueError("unsupported expression")


def class_defaults(config_manager_path):
    tree = ast.parse(Path(config_manager_path).read_text())
    defaults = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    try:
                        defaults[item.targets[0].id] = _safe_eval(item.value)
                    except (ValueError, TypeError, KeyError):
                        continue
    return defaults


def convert_value(raw, default):
    if isinstance(default, bool):
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default
    if isinstance(default, (list, tuple, dict, set)):
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                value = None
        if isinstance(value, type(default)):
            return value
        if isinstance(default, list) and value is None:
            return [part.strip() for part in raw.split(",") if part.strip()]
        return default
    if default is None:
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw
    return raw


def convert_values(legacy, defaults):
    converted = {}
    unsupported = {}
    for old_key, raw in legacy.items():
        if raw is None:
            continue
        new_key = ALIASES.get(old_key, old_key)
        if new_key == "SHOW_CLOUD_LINK" and old_key == "DISABLE_DRIVE_LINK":
            converted[new_key] = not convert_value(raw, False)
            continue
        if new_key not in defaults:
            unsupported[old_key] = raw
            continue
        converted[new_key] = convert_value(raw, defaults[new_key])
    return converted, unsupported


def write_config(legacy_path, sample_path, config_manager_path, output_path):
    from dotenv import dotenv_values

    if not Path(sample_path).is_file():
        raise FileNotFoundError(sample_path)
    legacy = dotenv_values(legacy_path)
    defaults = class_defaults(config_manager_path)
    converted, unsupported = convert_values(legacy, defaults)
    lines = [
        "# Generated from the existing protected config.env; review aliases before deployment.",
        "from config_sample import *",
        "",
    ]
    for key, value in sorted(converted.items()):
        lines.append(f"{key} = {value!r}")
    lines.extend(["", "# Legacy-only settings retained for reference; upstream may not support them.", f"LEGACY_CONFIG = {unsupported!r}", ""])
    path = Path(output_path)
    path.write_text("\n".join(lines))
    path.chmod(0o600)
    return converted, unsupported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_env")
    parser.add_argument("config_sample")
    parser.add_argument("config_manager")
    parser.add_argument("output")
    args = parser.parse_args()
    converted, unsupported = write_config(args.legacy_env, args.config_sample, args.config_manager, args.output)
    print(f"Generated {args.output}: {len(converted)} supported settings mapped; {len(unsupported)} legacy-only settings retained.")
    print("Values were not printed.")


if __name__ == "__main__":
    main()
