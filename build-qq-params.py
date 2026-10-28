"""从 NapCat 的 bundle 里抠出每个动作的真实参数表。

产出 `rin/rin/tools/qq_params.json`：{动作名: {summary, description, example, params}}。
不是手写的：`set_qq_profile` 到底收哪些字段、`personal_note` 是签名还是说明，
都从部署的那个构建里读出来，省得再靠猜。

继承要跟着走：`send_private_msg` 这类是 `extends Dy`，payloadSchema 在基类上。

    python build-qq-params.py <napcat.mjs> <qq_params.json>
"""

import io
import json
import re
import sys

DESC = re.compile(r'description:\s*"([^"]*)"')
DEFAULT = re.compile(r'default:\s*([^,}\n]+)')
TYPE = re.compile(r'p\.(String|Number|Boolean|Any|Object|Array|Union|Null|Ref)\(')


def brace_block(text, start):
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return ""


def name_table(text):
    pairs = {}
    for match in re.finditer(r'(\w+):\s*"([a-z0-9_]{3,})"', text):
        if "_" in match.group(2):
            pairs.setdefault(match.group(1), match.group(2))
    return pairs


def object_schemas(text):
    out = {}
    for match in re.finditer(r'const (\w+) = p\.Object\(', text):
        var = match.group(1)
        brace = text.index("{", match.end() - 1)
        body = brace_block(text, brace)
        params, seen = [], set()
        for field in re.finditer(r'(?m)^\s{2,6}(\w+):\s*p\.', body):
            name = field.group(1)
            if name in seen:
                continue
            seen.add(name)
            head = body[field.start():field.start() + 400]
            optional = bool(re.match(r'\s*\w+:\s*p\.Optional', body[field.start():]))
            type_match = TYPE.search(head)
            desc = DESC.search(head)
            default = DEFAULT.search(head)
            params.append({"name": name, "optional": optional,
                           "type": type_match.group(1) if type_match else "?",
                           "desc": desc.group(1) if desc else "",
                           "default": default.group(1).strip() if default else ""})
        if params:
            out[var] = params
    return out


def class_table(text):
    """类名 → {extends, prop, schema, summary, description, example}"""
    table = {}
    for match in re.finditer(r'class (\w+) extends (\w+) \{', text):
        name, base = match.group(1), match.group(2)
        body = brace_block(text, match.end() - 1)
        if "actionName = re." not in body and "payloadSchema" not in body:
            continue
        prop = re.search(r'actionName = re\.(\w+);', body)
        schema = re.search(r'payloadSchema = (\w+);', body)
        summary = re.search(r'actionSummary = "([^"]*)"', body)
        description = re.search(r'actionDescription = "([^"]*)"', body)
        example = re.search(r'payloadExample = (\{.*?\n\s{0,6}\};)', body, re.S)
        table[name] = {"extends": base,
                       "prop": prop.group(1) if prop else "",
                       "schema": schema.group(1) if schema else "",
                       "summary": summary.group(1) if summary else "",
                       "description": description.group(1) if description else "",
                       "example": example.group(1) if example else ""}
    return table


def resolve(name, table, depth=0):
    """顺着继承往上找 payloadSchema；没有也不影响，摘要照样有用。"""
    item = table.get(name)
    if not item or depth > 6:
        return None
    if item["schema"]:
        return item
    parent = resolve(item["extends"], table, depth + 1)
    if parent is None:
        return item
    merged = dict(item)
    merged["schema"] = parent["schema"]
    for key in ("summary", "description", "example"):
        if not merged[key]:
            merged[key] = parent[key]
    return merged


def main():
    source, target = sys.argv[1], sys.argv[2]
    text = io.open(source, encoding="utf-8", errors="replace").read()
    names = name_table(text)
    schemas = object_schemas(text)
    classes = class_table(text)
    found = {}
    for name, item in classes.items():
        resolved = resolve(name, classes)
        if not resolved or not resolved["prop"]:
            continue
        action = names.get(resolved["prop"])
        if not action:
            continue
        params = schemas.get(resolved["schema"], [])
        found[action] = {"summary": resolved["summary"],
                         "description": resolved["description"],
                         "example": resolved["example"],
                         "params": params}
    io.open(target, "w", encoding="utf-8", newline="\n").write(
        json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True))
    print("actions=%d with_params=%d" % (
        len(found), sum(1 for item in found.values() if item["params"])))


if __name__ == "__main__":
    main()
