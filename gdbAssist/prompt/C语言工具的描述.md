# 任务：写一个C/C++编程语言，GDB调试使用的脚本工具gdbAssist
gdb能够使用x/nx address 命令打印address下连续n个word的值。假设使用x/nx var命令，其中var是某个结构体或某个结构体对应的指针，gdbAssist能够展示输入的值到平铺的结构体var中
例如：
'''
class Classb {
  uint32_t a;
  uint32_t *b;
  uint32_t c[2];
};
typedef {
    uint32_t d;
    char e;
    Classb b;
} StructC;
typedef {
    uint32_t a;
    Classb b
    StructC c;
} var;
'''
假设var的声明 var tmp或var *tmp1;
使用命令(假设tmp和tmp1的储存地址都是0xf0000000)
x/16x &tmp 或 x/16x tmp1
回显(支持比结构体大的打印)：
0xf0000000： 0x00000001 0x00000002 0x00000003 0x00000004 0x00000005 0x00000006 0x00000007 0x00000008
0xf0000020： 0x00000009 0x0000000a 0x0000000b 0x0000000c 0x0000000d 0x0000000e 0x0000000f 0x00000010
gdbAssist输出结果(以tmp为例)：
var tmp:
  .a = 0x00000001
  Classb b:
    .a = 0x00000002
    .b = 0x0000000400000003
    .c[0] = 0x00000005
    .c[1] = 0x00000006
  StructC c:
    .d = 0x00000007
    .e = 0x08
    Classb b:
      .a = 0x00000009
      .b = 0x0000000b0000000a
      .c[0] = 0x0000000c
      .c[1] = 0x0000000d

## 支持特性
1. 支持Makefile, CMake, Bazel的构建下找到实际编译的源码，或先编译后想方法给出参与编译的文件
2. 自然对齐情况
3. bash脚本，支持windows和linux下运行
4. 输入：文本文件，里面写了gdb回显，自动从编译的cmake产物/makefile产物指示参与编译的路径
5. 输出：文本文件，在同一级目录，显示结果
6. 最大支持64层嵌套展开，64层后不展开
7. 示例的结构体和类定义如有不对，也请更正
## 其他要求，调用skills里的功能，深度思考，自我测试并纠正问题，给个脚本工具
