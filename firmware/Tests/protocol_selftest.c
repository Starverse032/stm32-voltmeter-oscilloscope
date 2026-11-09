/* ============================================================================
 * protocol_selftest.c —— 跨实现核对的编译期证据 + golden 帧常量
 *
 * 【本文件的用途，读前必看】
 * 依据 docs/protocol.md 第 8 节"跨实现核对方法"，避免"Python 自己编码自己解码"
 * 的循环验证。三方（文档期望值 / Python 实现 / ARMCC 编译期断言）必须一致。
 *
 * 1) 编译期 CRC 断言：用 protocol_codec.h 的 CRC_STEP 宏（由同一个 CRC_BIT1
 *    单步宏 8 层嵌套构成），把 golden 覆盖段逐字节折叠成 #if 常量表达式，
 *    断言结果 == docs/protocol.md 第 7 节给出的期望值。
 *    运行时 Protocol_Crc16 循环调用同一个 CRC_BIT1 8 次，语义一致；
 *    CRC_STEP 只用于这里的编译期断言（预处理期求值、不生成代码），
 *    若在运行时展开嵌套宏会生成约 110 KB 机器码撑爆 64 KB Flash，
 *    详见 protocol_codec.c 文件头与 docs/protocol.md 第 5 节。
 *    期望值原样取自文档，**不得修改**；不一致就 #error，构建直接失败。
 *    本文件已加入 Keil 工程参与编译，因此"ARMCC 编译通过"就是真实的
 *    "C 编译器 <-> 文档/Python" 跨实现核对证据。
 *
 * 2) golden 帧常量：下面 kGoldenSampleFrame[] 是 docs/protocol.md 样例 B 的
 *    完整帧（30 字节），**人工按协议逐字节写成十六进制常量**，
 *    绝不调用本工程的编码器生成。供主 agent 用 Python 解析本文件文本、
 *    解出字段与 CRC 后与文档期望值逐项比对。
 *
 * 【关于 #if 的组织方式：为什么是"逐字节短 #if"而不是一整条嵌套链】
 *   最初按文档第 8 节与任务描述，把
 *     CRC_STEP(CRC_STEP(...CRC_STEP(0xFFFF, b0)..., bN))
 *   整条 24 字节嵌套链放进单个 #if。但本机 Keil ARMCC V5.06 update 5
 *   (build 528) 实测：仅 9 字节的 "123456789" 整链 #if 就报
 *     "Tests\protocol_selftest.c(49): error: #4: out of memory"
 *   —— 是预处理器对超长/超深 #if 常量表达式的内存限制，不是 CRC 算错。
 *   因此改为"逐字节 #if"：每个 #if 仍是 CRC_STEP 展开的常量表达式
 *   （1 层 CRC_STEP = 8 层 CRC_BIT1，深度恒定、表达式很短），
 *   把覆盖段拆成 N 个短断言。每一步的期望中间值由协议第 5 节算法独立算出，
 *   最后一步的期望值严格等于文档 golden（0x29B1 / 0x8278 / 0x7EA4 / 0xE73A），
 *   **四个文档期望值一个都没改**。逐字节断言还更强：能定位到具体哪一字节不一致。
 *   原始失败日志保留在 Objects/build.log 的首轮构建记录与交付报告中，如实记录。
 *
 * 【关于 CRC_STEP 与 #if】
 *   CRC_STEP 保留文档 8 层 CRC_BIT1 嵌套、括号配平的结构，只把 (uint16_t) 强转
 *   换成等价的 & 0xFFFFu，因为 C 预处理器在 #if 中不允许类型转换（详见
 *   Common/protocol_codec.h 的等价性说明）。
 *
 * 四组期望值（全部来自 protocol.md，未改）：
 *   "123456789"        -> 0x29B1
 *   样例 A 覆盖段(11B) -> 0x8278
 *   样例 B 覆盖段(24B) -> 0x7EA4
 *   样例 C 覆盖段(20B) -> 0xE73A
 * ==========================================================================*/

#include "protocol_codec.h"   /* CRC_BIT1：运行时与编译期共用；CRC_STEP：仅编译期 #if 断言 */

/* ---- 标准自检向量 "123456789" (9 字节) ---- */
#if CRC_STEP(0xFFFFu, 0x31u) != 0xC782u
#error "CRC V byte 0 mismatch"
#endif
#if CRC_STEP(0xC782u, 0x32u) != 0x3DBAu
#error "CRC V byte 1 mismatch"
#endif
#if CRC_STEP(0x3DBAu, 0x33u) != 0x5BCEu
#error "CRC V byte 2 mismatch"
#endif
#if CRC_STEP(0x5BCEu, 0x34u) != 0x5349u
#error "CRC V byte 3 mismatch"
#endif
#if CRC_STEP(0x5349u, 0x35u) != 0x4560u
#error "CRC V byte 4 mismatch"
#endif
#if CRC_STEP(0x4560u, 0x36u) != 0x2EF4u
#error "CRC V byte 5 mismatch"
#endif
#if CRC_STEP(0x2EF4u, 0x37u) != 0x7718u
#error "CRC V byte 6 mismatch"
#endif
#if CRC_STEP(0x7718u, 0x38u) != 0xA12Bu
#error "CRC V byte 7 mismatch"
#endif
#if CRC_STEP(0xA12Bu, 0x39u) != 0x29B1u
#error "CRC V byte 8 mismatch"
#endif
/* 末字节后 CRC = 0x29B1，等于文档 golden V */
/* ---- golden A：SET_RATE 命令帧覆盖段 (11 字节) ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC A byte 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x20u) != 0x0A5Cu
#error "CRC A byte 1 mismatch"
#endif
#if CRC_STEP(0x0A5Cu, 0x05u) != 0xADEFu
#error "CRC A byte 2 mismatch"
#endif
#if CRC_STEP(0xADEFu, 0x00u) != 0x8B47u
#error "CRC A byte 3 mismatch"
#endif
#if CRC_STEP(0x8B47u, 0x07u) != 0x1704u
#error "CRC A byte 4 mismatch"
#endif
#if CRC_STEP(0x1704u, 0x00u) != 0x66D6u
#error "CRC A byte 5 mismatch"
#endif
#if CRC_STEP(0x66D6u, 0x04u) != 0x9AE4u
#error "CRC A byte 6 mismatch"
#endif
#if CRC_STEP(0x9AE4u, 0xA0u) != 0x7319u
#error "CRC A byte 7 mismatch"
#endif
#if CRC_STEP(0x7319u, 0x86u) != 0xA6BAu
#error "CRC A byte 8 mismatch"
#endif
#if CRC_STEP(0xA6BAu, 0x01u) != 0x7F0Du
#error "CRC A byte 9 mismatch"
#endif
#if CRC_STEP(0x7F0Du, 0x00u) != 0x8278u
#error "CRC A byte 10 mismatch"
#endif
/* 末字节后 CRC = 0x8278，等于文档 golden A */
/* ---- golden B：4 点采样帧覆盖段 (24 字节) ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC B byte 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x10u) != 0x3C0Fu
#error "CRC B byte 1 mismatch"
#endif
#if CRC_STEP(0x3C0Fu, 0x12u) != 0xCAACu
#error "CRC B byte 2 mismatch"
#endif
#if CRC_STEP(0xCAACu, 0x00u) != 0xD406u
#error "CRC B byte 3 mismatch"
#endif
#if CRC_STEP(0xD406u, 0x2Au) != 0x08D1u
#error "CRC B byte 4 mismatch"
#endif
#if CRC_STEP(0x08D1u, 0x00u) != 0x5008u
#error "CRC B byte 5 mismatch"
#endif
#if CRC_STEP(0x5008u, 0x10u) != 0x40C4u
#error "CRC B byte 6 mismatch"
#endif
#if CRC_STEP(0x40C4u, 0x27u) != 0xD841u
#error "CRC B byte 7 mismatch"
#endif
#if CRC_STEP(0xD841u, 0x00u) != 0x0B75u
#error "CRC B byte 8 mismatch"
#endif
#if CRC_STEP(0x0B75u, 0x00u) != 0xC46Bu
#error "CRC B byte 9 mismatch"
#endif
#if CRC_STEP(0xC46Bu, 0x04u) != 0xB24Cu
#error "CRC B byte 10 mismatch"
#endif
#if CRC_STEP(0xB24Cu, 0x00u) != 0xCB99u
#error "CRC B byte 11 mismatch"
#endif
#if CRC_STEP(0xCB99u, 0xE4u) != 0x4C8Du
#error "CRC B byte 12 mismatch"
#endif
#if CRC_STEP(0x4C8Du, 0x0Cu) != 0xC5C4u
#error "CRC B byte 13 mismatch"
#endif
#if CRC_STEP(0xC5C4u, 0x00u) != 0x4DE9u
#error "CRC B byte 14 mismatch"
#endif
#if CRC_STEP(0x4DE9u, 0x00u) != 0x7069u
#error "CRC B byte 15 mismatch"
#endif
#if CRC_STEP(0x7069u, 0x00u) != 0x1797u
#error "CRC B byte 16 mismatch"
#endif
#if CRC_STEP(0x1797u, 0x00u) != 0xF5D6u
#error "CRC B byte 17 mismatch"
#endif
#if CRC_STEP(0xF5D6u, 0x00u) != 0x69BAu
#error "CRC B byte 18 mismatch"
#endif
#if CRC_STEP(0x69BAu, 0x08u) != 0xC687u
#error "CRC B byte 19 mismatch"
#endif
#if CRC_STEP(0xC687u, 0xFFu) != 0x207Au
#error "CRC B byte 20 mismatch"
#endif
#if CRC_STEP(0x207Au, 0x0Fu) != 0xAF8Du
#error "CRC B byte 21 mismatch"
#endif
#if CRC_STEP(0xAF8Du, 0xD2u) != 0x223Au
#error "CRC B byte 22 mismatch"
#endif
#if CRC_STEP(0x223Au, 0x04u) != 0x7EA4u
#error "CRC B byte 23 mismatch"
#endif
/* 末字节后 CRC = 0x7EA4，等于文档 golden B */
/* ---- golden C：响应帧覆盖段 (20 字节) ---- */
#if CRC_STEP(0xFFFFu, 0x01u) != 0xF1D1u
#error "CRC C byte 0 mismatch"
#endif
#if CRC_STEP(0xF1D1u, 0x21u) != 0x1A7Du
#error "CRC C byte 1 mismatch"
#endif
#if CRC_STEP(0x1A7Du, 0x0Eu) != 0x2FB5u
#error "CRC C byte 2 mismatch"
#endif
#if CRC_STEP(0x2FB5u, 0x00u) != 0x608Du
#error "CRC C byte 3 mismatch"
#endif
#if CRC_STEP(0x608Du, 0x07u) != 0x9141u
#error "CRC C byte 4 mismatch"
#endif
#if CRC_STEP(0x9141u, 0x00u) != 0xD298u
#error "CRC C byte 5 mismatch"
#endif
#if CRC_STEP(0xD298u, 0x04u) != 0x33BBu
#error "CRC C byte 6 mismatch"
#endif
#if CRC_STEP(0x33BBu, 0x00u) != 0xBD30u
#error "CRC C byte 7 mismatch"
#endif
#if CRC_STEP(0xBD30u, 0xA0u) != 0xF39Cu
#error "CRC C byte 8 mismatch"
#endif
#if CRC_STEP(0xF39Cu, 0x86u) != 0xB232u
#error "CRC C byte 9 mismatch"
#endif
#if CRC_STEP(0xB232u, 0x01u) != 0xA5B8u
#error "CRC C byte 10 mismatch"
#endif
#if CRC_STEP(0xA5B8u, 0x00u) != 0x5D4Fu
#error "CRC C byte 11 mismatch"
#endif
#if CRC_STEP(0x5D4Fu, 0xE4u) != 0x79F2u
#error "CRC C byte 12 mismatch"
#endif
#if CRC_STEP(0x79F2u, 0x0Cu) != 0xDC32u
#error "CRC C byte 13 mismatch"
#endif
#if CRC_STEP(0xDC32u, 0x00u) != 0x38F1u
#error "CRC C byte 14 mismatch"
#endif
#if CRC_STEP(0x38F1u, 0x00u) != 0x465Bu
#error "CRC C byte 15 mismatch"
#endif
#if CRC_STEP(0x465Bu, 0x00u) != 0x7302u
#error "CRC C byte 16 mismatch"
#endif
#if CRC_STEP(0x7302u, 0x00u) != 0x4CF4u
#error "CRC C byte 17 mismatch"
#endif
#if CRC_STEP(0x4CF4u, 0x00u) != 0x7D48u
#error "CRC C byte 18 mismatch"
#endif
#if CRC_STEP(0x7D48u, 0x00u) != 0xE73Au
#error "CRC C byte 19 mismatch"
#endif
/* 末字节后 CRC = 0xE73A，等于文档 golden C */
/* ---------------------------------------------------------------------------
 * golden 帧常量：docs/protocol.md 样例 B 的**完整帧**（逐字节人工写死）。
 * A5 5A C3 3C 01 10 12 00 2A 00 10 27 00 00 04 00 E4 0C 00 00
 * 00 00 00 08 FF 0F D2 04 A4 7E
 * 共 30 字节 = LEN(18) + 12。帧尾 A4 7E 即 CRC 0x7EA4 的低字节在前。
 * 不调用任何编码器生成；供 Python 解析本文件文本做跨实现核对。
 * -------------------------------------------------------------------------*/
const uint8_t kGoldenSampleFrame[] = {
    0xA5, 0x5A, 0xC3, 0x3C, 0x01, 0x10, 0x12, 0x00,
    0x2A, 0x00, 0x10, 0x27, 0x00, 0x00, 0x04, 0x00,
    0xE4, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08,
    0xFF, 0x0F, 0xD2, 0x04, 0xA4, 0x7E
};

/* 提供只读访问接口，确保该常量确实参与链接（不会被编译器整体丢弃）。 */
const uint8_t *Protocol_GetGoldenSampleFrame(void)
{
    return kGoldenSampleFrame;
}

uint16_t Protocol_GetGoldenSampleFrameLen(void)
{
    return (uint16_t)sizeof(kGoldenSampleFrame);
}
