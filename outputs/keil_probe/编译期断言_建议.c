#include "protocol_codec.h"
/* 每步独立 #if：输入 crc 用字面常量，避免宏链嵌套导致 ARMCC 预处理器 out of memory */
/* 中间值由独立 Python 实现算出；最后一步的期望值原样取自 docs/protocol.md 第 7 节 */
int probe_dummy;

/* ---- golden V: 9 字节, 期望最终 CRC = 0x29B1 ---- */
#if CRC_STEP(0xFFFFu, 0x31u) != 0xC782u
#error "CRC V step 0 mismatch"
#endif
#if CRC_STEP(0xC782u, 0x32u) != 0x3DBAu
#error "CRC V step 1 mismatch"
#endif
#if CRC_STEP(0x3DBAu, 0x33u) != 0x5BCEu
#error "CRC V step 2 mismatch"
#endif
#if CRC_STEP(0x5BCEu, 0x34u) != 0x5349u
#error "CRC V step 3 mismatch"
#endif
#if CRC_STEP(0x5349u, 0x35u) != 0x4560u
#error "CRC V step 4 mismatch"
#endif
#if CRC_STEP(0x4560u, 0x36u) != 0x2EF4u
#error "CRC V step 5 mismatch"
#endif
#if CRC_STEP(0x2EF4u, 0x37u) != 0x7718u
#error "CRC V step 6 mismatch"
#endif
#if CRC_STEP(0x7718u, 0x38u) != 0xA12Bu
#error "CRC V step 7 mismatch"
#endif
#if CRC_STEP(0xA12Bu, 0x39u) != 0x29B1u
#error "CRC V step 8 mismatch"
#endif
/* V 末步 = 0x29B1u == 文档期望 0x29B1u */

/* ---- golden A: 11 字节, 期望最终 CRC = 0x8278 ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC A step 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x20u) != 0x0A5Cu
#error "CRC A step 1 mismatch"
#endif
#if CRC_STEP(0x0A5Cu, 0x05u) != 0xADEFu
#error "CRC A step 2 mismatch"
#endif
#if CRC_STEP(0xADEFu, 0x00u) != 0x8B47u
#error "CRC A step 3 mismatch"
#endif
#if CRC_STEP(0x8B47u, 0x07u) != 0x1704u
#error "CRC A step 4 mismatch"
#endif
#if CRC_STEP(0x1704u, 0x00u) != 0x66D6u
#error "CRC A step 5 mismatch"
#endif
#if CRC_STEP(0x66D6u, 0x04u) != 0x9AE4u
#error "CRC A step 6 mismatch"
#endif
#if CRC_STEP(0x9AE4u, 0xA0u) != 0x7319u
#error "CRC A step 7 mismatch"
#endif
#if CRC_STEP(0x7319u, 0x86u) != 0xA6BAu
#error "CRC A step 8 mismatch"
#endif
#if CRC_STEP(0xA6BAu, 0x01u) != 0x7F0Du
#error "CRC A step 9 mismatch"
#endif
#if CRC_STEP(0x7F0Du, 0x00u) != 0x8278u
#error "CRC A step 10 mismatch"
#endif
/* A 末步 = 0x8278u == 文档期望 0x8278u */

/* ---- golden B: 24 字节, 期望最终 CRC = 0x7EA4 ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC B step 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x10u) != 0x3C0Fu
#error "CRC B step 1 mismatch"
#endif
#if CRC_STEP(0x3C0Fu, 0x12u) != 0xCAACu
#error "CRC B step 2 mismatch"
#endif
#if CRC_STEP(0xCAACu, 0x00u) != 0xD406u
#error "CRC B step 3 mismatch"
#endif
#if CRC_STEP(0xD406u, 0x2Au) != 0x08D1u
#error "CRC B step 4 mismatch"
#endif
#if CRC_STEP(0x08D1u, 0x00u) != 0x5008u
#error "CRC B step 5 mismatch"
#endif
#if CRC_STEP(0x5008u, 0x10u) != 0x40C4u
#error "CRC B step 6 mismatch"
#endif
#if CRC_STEP(0x40C4u, 0x27u) != 0xD841u
#error "CRC B step 7 mismatch"
#endif
#if CRC_STEP(0xD841u, 0x00u) != 0x0B75u
#error "CRC B step 8 mismatch"
#endif
#if CRC_STEP(0x0B75u, 0x00u) != 0xC46Bu
#error "CRC B step 9 mismatch"
#endif
#if CRC_STEP(0xC46Bu, 0x04u) != 0xB24Cu
#error "CRC B step 10 mismatch"
#endif
#if CRC_STEP(0xB24Cu, 0x00u) != 0xCB99u
#error "CRC B step 11 mismatch"
#endif
#if CRC_STEP(0xCB99u, 0xE4u) != 0x4C8Du
#error "CRC B step 12 mismatch"
#endif
#if CRC_STEP(0x4C8Du, 0x0Cu) != 0xC5C4u
#error "CRC B step 13 mismatch"
#endif
#if CRC_STEP(0xC5C4u, 0x00u) != 0x4DE9u
#error "CRC B step 14 mismatch"
#endif
#if CRC_STEP(0x4DE9u, 0x00u) != 0x7069u
#error "CRC B step 15 mismatch"
#endif
#if CRC_STEP(0x7069u, 0x00u) != 0x1797u
#error "CRC B step 16 mismatch"
#endif
#if CRC_STEP(0x1797u, 0x00u) != 0xF5D6u
#error "CRC B step 17 mismatch"
#endif
#if CRC_STEP(0xF5D6u, 0x00u) != 0x69BAu
#error "CRC B step 18 mismatch"
#endif
#if CRC_STEP(0x69BAu, 0x08u) != 0xC687u
#error "CRC B step 19 mismatch"
#endif
#if CRC_STEP(0xC687u, 0xFFu) != 0x207Au
#error "CRC B step 20 mismatch"
#endif
#if CRC_STEP(0x207Au, 0x0Fu) != 0xAF8Du
#error "CRC B step 21 mismatch"
#endif
#if CRC_STEP(0xAF8Du, 0xD2u) != 0x223Au
#error "CRC B step 22 mismatch"
#endif
#if CRC_STEP(0x223Au, 0x04u) != 0x7EA4u
#error "CRC B step 23 mismatch"
#endif
/* B 末步 = 0x7EA4u == 文档期望 0x7EA4u */

/* ---- golden C: 20 字节, 期望最终 CRC = 0xE73A ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC C step 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x21u) != 0x1A7Du
#error "CRC C step 1 mismatch"
#endif
#if CRC_STEP(0x1A7Du, 0x0Eu) != 0x2FB5u
#error "CRC C step 2 mismatch"
#endif
#if CRC_STEP(0x2FB5u, 0x00u) != 0x608Du
#error "CRC C step 3 mismatch"
#endif
#if CRC_STEP(0x608Du, 0x07u) != 0x9141u
#error "CRC C step 4 mismatch"
#endif
#if CRC_STEP(0x9141u, 0x00u) != 0xD298u
#error "CRC C step 5 mismatch"
#endif
#if CRC_STEP(0xD298u, 0x04u) != 0x33BBu
#error "CRC C step 6 mismatch"
#endif
#if CRC_STEP(0x33BBu, 0x00u) != 0xBD30u
#error "CRC C step 7 mismatch"
#endif
#if CRC_STEP(0xBD30u, 0xA0u) != 0xF39Cu
#error "CRC C step 8 mismatch"
#endif
#if CRC_STEP(0xF39Cu, 0x86u) != 0xB232u
#error "CRC C step 9 mismatch"
#endif
#if CRC_STEP(0xB232u, 0x01u) != 0xA5B8u
#error "CRC C step 10 mismatch"
#endif
#if CRC_STEP(0xA5B8u, 0x00u) != 0x5D4Fu
#error "CRC C step 11 mismatch"
#endif
#if CRC_STEP(0x5D4Fu, 0xE4u) != 0x79F2u
#error "CRC C step 12 mismatch"
#endif
#if CRC_STEP(0x79F2u, 0x0Cu) != 0xDC32u
#error "CRC C step 13 mismatch"
#endif
#if CRC_STEP(0xDC32u, 0x00u) != 0x38F1u
#error "CRC C step 14 mismatch"
#endif
#if CRC_STEP(0x38F1u, 0x00u) != 0x465Bu
#error "CRC C step 15 mismatch"
#endif
#if CRC_STEP(0x465Bu, 0x00u) != 0x7302u
#error "CRC C step 16 mismatch"
#endif
#if CRC_STEP(0x7302u, 0x00u) != 0x4CF4u
#error "CRC C step 17 mismatch"
#endif
#if CRC_STEP(0x4CF4u, 0x00u) != 0x7D48u
#error "CRC C step 18 mismatch"
#endif
#if CRC_STEP(0x7D48u, 0x00u) != 0xE73Au
#error "CRC C step 19 mismatch"
#endif
/* C 末步 = 0xE73Au == 文档期望 0xE73Au */

