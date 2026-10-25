// A valid aggregate isolates eager debugger traversal from corrupt target data.
// Build with -O0 -g -DMEMBER_COUNT=N. The inferior never allocates dynamically.
#ifndef MEMBER_COUNT
#define MEMBER_COUNT 8192
#endif

struct Leaf {
  int first;
  int second;
};

struct Payload {
  Leaf items[MEMBER_COUNT];
};

int main() {
  Payload payload = {};
  payload.items[0].first = 42;
  asm volatile("" : : "g"(&payload) : "memory"); // BREAK_HERE
  payload.items[0].first = 43;
  asm volatile("" : : "g"(&payload) : "memory"); // BREAK_AGAIN
  return payload.items[0].first == 43 ? 0 : 1;
}
