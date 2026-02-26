#include <stdint.h>
typedef struct {
    uint32_t id;
    uint32_t value;
} Item;

typedef struct {
    uint32_t count;
    Item items[3];
} Container;
