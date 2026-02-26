/* test_structs.h - Test structure definitions for gdbAssist */
#include <stdint.h>

class Classb {
  uint32_t a;
  uint32_t *b;
  uint32_t c[2];
};

typedef struct {
    uint32_t d;
    char e;
    Classb b;
} StructC;

typedef struct {
    uint32_t a;
    Classb b;
    StructC c;
} var;
