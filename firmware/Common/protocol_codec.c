#include "protocol_codec.h"

/* ============================================================================
 * protocol_codec.c —— CRC、小端、通用帧封装的运行时实现
 *
 * 跨实现核对关系（docs/protocol.md 第 8 节）：
 *   Tests/protocol_selftest.c 用 #if 编译期常量断言，把 CRC_STEP（8 层 CRC_BIT1
 *   嵌套）对固定 golden 向量求值，与文档/Python 比对；本文件的运行时函数用
 *   **同一个 CRC_BIT1 单步宏**循环 8 次，两者语义由同一个宏定义保证一致。
 *
 * 【重要 · 为什么运行时不直接展开 CRC_STEP】
 *   CRC_BIT1(c) 是三元表达式，(c) 每层出现 3 次；8 层嵌套 = 3^8 份表达式。
 *   若在运行时函数里写 crc = CRC_STEP(crc, *data++)，ARMCC 会把它编译成约
 *   110 KB 机器码，直接撑爆 STM32F103C8 的 64 KB Flash（本机实测
 *   protocol_codec.o code = 112640 B，导致 L6406E/L6407E 链接失败）。
 *   编译期 #if 用该宏没有问题：预处理期求值，不生成任何代码。
 *   改成循环调用单步宏后，同一函数实测 code = 58 B（fromelf -z）。
 * ==========================================================================*/

uint16_t Protocol_Crc16(const uint8_t *data, uint32_t len)
{
    uint16_t crc = 0xFFFFu;
    uint32_t n;
    uint8_t bit;

    for (n = 0; n < len; n++)
    {
        /* 先把当前字节放到 CRC 高 8 位异或，再做 8 次"左移 + 条件异或 0x1021" */
        crc ^= (uint16_t)((uint16_t)data[n] << 8);
        for (bit = 0; bit < 8; bit++)
        {
            crc = (uint16_t)CRC_BIT1(crc);
        }
    }
    return crc;
}

void Protocol_WriteU16LE(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

void Protocol_WriteU32LE(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

uint16_t Protocol_ReadU16LE(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

uint32_t Protocol_ReadU32LE(const uint8_t *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

uint8_t *Protocol_BeginFrame(uint8_t *out, uint8_t type, uint8_t seq, uint16_t len)
{
    out[0] = PROTO_MAGIC0;
    out[1] = PROTO_MAGIC1;
    out[2] = PROTO_MAGIC2;
    out[3] = PROTO_MAGIC3;
    out[4] = PROTO_VER;
    out[5] = type;
    out[6] = (uint8_t)(len & 0xFFu);            /* LEN 低字节在前 */
    out[7] = (uint8_t)((len >> 8) & 0xFFu);
    out[8] = seq;
    out[9] = 0x00u;                              /* FLAGS 保留，必须为 0 */
    return out + PROTO_HEADER_LEN;                /* payload 起点 = 偏移 10 */
}

uint16_t Protocol_FinishFrame(uint8_t *out, uint16_t len)
{
    /* CRC 覆盖 VER(偏移 4) .. PAYLOAD 末，共 6+LEN 字节；不含 MAGIC，也不含帧尾 CRC。
     * 不含 MAGIC 让接收端滑动搜索到 MAGIC 后能直接校验其余全部字段。 */
    uint16_t crc = Protocol_Crc16(out + 4, (uint32_t)(6u + len));
    out[PROTO_HEADER_LEN + len]      = (uint8_t)(crc & 0xFFu);        /* 低字节在前 */
    out[PROTO_HEADER_LEN + len + 1]  = (uint8_t)((crc >> 8) & 0xFFu);
    return (uint16_t)(len + PROTO_HEADER_LEN + PROTO_CRC_LEN);       /* = LEN + 12 */
}

uint16_t Protocol_BuildFrame(uint8_t *out, uint8_t type, uint8_t seq,
                             const uint8_t *payload, uint16_t len)
{
    uint8_t *p = Protocol_BeginFrame(out, type, seq, len);
    uint16_t i;
    for (i = 0; i < len; i++)
    {
        p[i] = payload[i];
    }
    return Protocol_FinishFrame(out, len);
}
