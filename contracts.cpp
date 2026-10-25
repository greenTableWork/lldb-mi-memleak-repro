#ifndef MEMBER_COUNT
#define MEMBER_COUNT 256
#endif

struct Leaf {
  int first;
  int second;
};
struct Payload {
  Leaf items[MEMBER_COUNT];
};
struct Node {
  int value;
  Node *next;
};
struct Sequence {
  int count;
  int data[3];
};
struct Changing {
  int mode;
  Leaf leaf;
  Payload payload;
};

int main() {
  Payload payload = {};
  Leaf alternate = {99, 100};
  Leaf *pointer = &payload.items[0];
  Node cycle = {7, nullptr};
  cycle.next = &cycle;
  Sequence numbers{3, {10, 20, 30}};
  Changing changing = {};
  payload.items[0].first = 42;
  {
    int scoped = 17;
    asm volatile("" : : "g"(&payload), "g"(&scoped) : "memory"); // BREAK_HERE
    payload.items[0].first = 43;
    pointer = &alternate;
    numbers.count = 1;
    changing.mode = 1;
    asm volatile("" : : "g"(&payload), "g"(&pointer) : "memory"); // BREAK_CHANGED
    pointer = nullptr;
    asm volatile("" : : "g"(&pointer) : "memory"); // BREAK_NULL
    pointer = &payload.items[0];
    numbers.count = 3;
    numbers.data[1] = 40;
    numbers.data[2] = 50;
    asm volatile("" : : "g"(&pointer), "g"(&numbers) : "memory"); // BREAK_RESTORED
  }
  asm volatile("" : : "g"(&payload), "g"(&cycle), "g"(&changing) : "memory"); // BREAK_OUT_OF_SCOPE
  return 0;
}
