"""Deterministic synthetic children without a platform C++ library dependency."""


class SequenceProvider:
    def __init__(self, value, _):
        self.value = value
        self.update()

    def update(self):
        self.count = self.value.GetChildMemberWithName("count").GetValueAsUnsigned()

    def num_children(self, max_count=None):
        return min(self.count, 3)

    def has_children(self):
        return self.num_children() != 0

    def get_child_index(self, name):
        return int(name.strip("[]"))

    def get_child_at_index(self, index):
        if index >= self.num_children():
            return None
        return self.value.GetChildMemberWithName("data").GetChildAtIndex(index)


class ChangingProvider:
    def __init__(self, value, _):
        self.value = value
        self.update()

    def update(self):
        mode = self.value.GetChildMemberWithName("mode").GetValueAsUnsigned()
        self.member = "payload" if mode else "leaf"

    def num_children(self, max_count=None):
        return 1

    def has_children(self):
        return True

    def get_child_index(self, name):
        return 0 if name == "value" else -1

    def get_child_at_index(self, index):
        if index != 0:
            return None
        return self.value.GetChildMemberWithName(self.member).Clone("value")


def __lldb_init_module(debugger, _):
    debugger.HandleCommand("type synthetic add -l contracts_formatters.SequenceProvider Sequence")
    debugger.HandleCommand("type synthetic add -l contracts_formatters.ChangingProvider Changing")
