"""MI contract checks against a real stopped inferior; no debugger mocks."""

import json
from pathlib import Path
import re


def run(command, wait_for, source, count):
    checks = []

    def check(label, condition, result):
        checks.append({"check": label, "passed": bool(condition), "result": result})

    def cmd(text):
        return command(text, allow_error=True)

    def done(text, label=None):
        result = cmd(text)
        check(label or text, "^done" in result or
              (text == "-exec-continue" and "^running" in result), result)
        return result

    def absent(name):
        result = cmd(f"-var-evaluate-expression {name}")
        check(f"deleted {name} is inaccessible", "^error" in result, result)

    def deleted(name, number, children_only=False):
        result = cmd(f"-var-delete {'-c ' if children_only else ''}{name}")
        check(f"delete {name}: {number} objects",
              f'ndeleted="{number}"' in result, result)

    def empty(name="root"):
        result = cmd(f"-var-update 1 {name}")
        check(f"unchanged {name} has no notifications", "changelist=[]" in result,
              result)

    def changed(name, member, value=None, mode=1, scope=True):
        result = cmd(f"-var-update {mode} {name}")
        # One record per name, and test the field in that record, not a sibling.
        records = re.findall(r'\{((?:[^{}"]|"(?:\\.|[^"\\])*")*)\}', result)
        matching = [r for r in records if f'name="{member}"' in r]
        ok = len(matching) == 1 and f'in_scope="{str(scope).lower()}"' in matching[0]
        if value is not None:
            ok = ok and f'value="{value}"' in matching[0]
        if mode == 0 or not scope:
            ok = ok and 'value="' not in matching[0]
        check(f"update {name} reports {member} once, value={value}, scope={scope}",
              ok, result)
        return result

    def expand(root, expression="payload"):
        done(f'-var-create {root} * "{expression}"')
        done(f"-var-list-children 0 {root} 0 1")
        done(f"-var-list-children 0 {root}.items 0 1")
        done(f"-var-list-children 1 {root}.items.[0] 0 2")

    formatter = source.with_name("contracts_formatters.py")
    formatter.write_bytes(Path(__file__).with_name(
        "contracts_formatters.py").read_bytes())
    done("-interpreter-exec console " +
         json.dumps("command script import " + json.dumps(str(formatter))))

    # Parent relationships must not be inferred from user-controlled names.
    expand("tree")
    done("-var-create tree.unrelated * alternate")
    deleted("tree", 5)
    for name in ("tree", "tree.items", "tree.items.[0]", "tree.items.[0].first",
                 "tree.items.[0].second"):
        absent(name)
    done("-var-evaluate-expression tree.unrelated")
    deleted("tree.unrelated", 1)
    result = cmd("-var-delete missing")
    check("deleting an unknown name fails", "^error" in result, result)
    done("-var-create collision.items * alternate")
    done("-var-create collision * payload")
    result = cmd("-var-list-children 0 collision 0 1")
    check("child registration cannot overwrite an independent root",
          "^error" in result, result)
    deleted("collision", 1)
    done("-var-evaluate-expression collision.items")
    deleted("collision.items", 1)
    result = cmd("-var-create invalid * no_such_variable")
    check("failed creation returns an error", "^error" in result, result)
    absent("invalid")

    expand("tree")
    deleted("tree.items", 3, children_only=True)
    done("-var-evaluate-expression tree.items")
    absent("tree.items.[0]")
    absent("tree.items.[0].first")
    deleted("tree.items", 0, children_only=True)
    done("-var-list-children 0 tree.items 0 1")
    deleted("tree.items.[0]", 1)
    done("-var-list-children 0 tree.items 0 1")
    deleted("tree", 3)

    expand("root")
    result = cmd("-var-create root * alternate")
    check("duplicate root creation fails", "^error" in result, result)
    done("-var-set-format root.items.[0].first hexadecimal")
    done("-var-list-children 0 root 0 1")
    done("-var-list-children 0 root.items 0 1")
    result = done("-var-list-children 1 root.items.[0] 0 2")
    check("re-listing preserves child format", 'value="0x2a"' in result, result)
    empty()

    # A small page at a high index proves there is no silent 64-child cap.
    if count > 128:
        result = done("-var-list-children 0 root.items 127 129")
        check("high child page includes both requested indices",
              'name="root.items.[127]"' in result and
              'name="root.items.[128]"' in result and 'numchild="2"' in result,
              result)
    result = done(f"-var-list-children 0 root.items {count} {count + 2}")
    check("range beyond end is empty", 'numchild="0"' in result, result)
    result = done("-var-list-children 0 root.items 2 1")
    check("reversed range is empty", 'numchild="0"' in result, result)
    result = cmd("-var-list-children 0 root.items 1")
    check("partial range is rejected", "^error" in result, result)

    # Root expressions return fresh SBValues. Only registered children should
    # be rebound to them, and evaluations must not retain persistent $N roots.
    expand("expr", "(payload)")
    done('-var-create scalar * "payload.items[0].first + 1"')
    done("-var-create ptr * pointer")
    done("-var-list-children 1 ptr 0 2")
    done('-var-create ptrExpr * "*pointer"')
    done("-var-list-children 1 ptrExpr 0 2")
    done("-var-create vec * numbers")
    done("-var-list-children 1 vec 0 3")
    done("-var-create dynamic * changing")
    done("-var-list-children 0 dynamic 0 1")
    done("-var-list-children 0 dynamic.value 0 2")
    done("-var-create scoped * scoped")
    done('-var-create scopedExpr * "scoped + 1"')
    done("-var-create ring * cycle")
    done("-var-list-children 0 ring 0 2")
    done("-var-list-children 0 ring.next 0 2")
    empty("ring")
    deleted("ring", 5)

    markers = {}
    for i, line in enumerate(source.read_text().splitlines(), 1):
        if "// BREAK_" in line and "// BREAK_HERE" not in line:
            marker = line.split("// ")[1].strip()
            markers[marker] = i
            done("-break-insert " + json.dumps(f"{source}:{i}"))

    def resume(marker):
        done("-exec-continue")
        stop = wait_for(lambda line: line.startswith("*stopped"))
        check(f"stop at {marker}",
              'reason="breakpoint-hit"' in stop and
              f'line="{markers[marker]}"' in stop, stop)

    resume("BREAK_CHANGED")
    # Formatting/evaluation before update must not consume the notification.
    done("-var-evaluate-expression root.items.[0].first")
    changed("root", "root.items.[0].first", "0x2b")
    empty()
    changed("expr", "expr.items.[0].first", "43")
    empty("expr")
    changed("scalar", "scalar", "44")
    empty("scalar")
    changed("ptr", "ptr.first", "99")
    empty("ptr")
    changed("ptrExpr", "ptrExpr.first", "99")
    empty("ptrExpr")
    changed("vec", "vec.[2]", scope=False)
    empty("vec")
    result = changed("dynamic", "dynamic.value", "{...}")
    check("changed child type invalidates its old descendants",
          'type_changed="true"' in result and 'new_type="Payload"' in result and
          'new_num_children="1"' in result, result)
    absent("dynamic.value.first")
    absent("dynamic.value.second")
    done("-var-list-children 0 dynamic.value 0 1")
    done("-var-evaluate-expression dynamic.value.items")
    empty("dynamic")

    # Assignments are reported once, with the requested output mode.
    for value, mode in ((44, 0), (45, 2), (46, 1)):
        done(f"-var-assign root.items.[0].first {value}")
        changed("root", "root.items.[0].first",
                hex(value) if mode else None, mode=mode)
        empty()
    # A no-op assignment must not erase a still-pending earlier change.
    done("-var-assign root.items.[0].first 47")
    done("-var-assign root.items.[0].first 47")
    changed("root", "root.items.[0].first", "0x2f")
    empty()
    # Updating the leaf of an expression must refresh its ancestor expression.
    changed("expr.items.[0].first", "expr.items.[0].first", "47")
    empty("expr")

    resume("BREAK_NULL")
    changed("ptr", "ptr.first", scope=False)
    empty("ptr")
    changed("ptrExpr", "ptrExpr.first", scope=False)
    empty("ptrExpr")
    resume("BREAK_RESTORED")
    changed("ptr", "ptr.first", "47")
    empty("ptr")
    changed("ptrExpr", "ptrExpr.first", "47")
    empty("ptrExpr")
    changed("vec", "vec.[2]", "50")
    empty("vec")
    resume("BREAK_OUT_OF_SCOPE")
    changed("scoped", "scoped", scope=False)
    empty("scoped")
    changed("scopedExpr", "scopedExpr", scope=False)
    empty("scopedExpr")
    # All roots must still be deletable after binding and scope changes.
    for name in ("root", "expr", "scalar", "ptr", "ptrExpr", "vec", "scoped",
                 "scopedExpr", "dynamic"):
        done(f"-var-delete {name}")
    return checks
