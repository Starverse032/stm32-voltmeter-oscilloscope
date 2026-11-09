#ifndef __PROTOCOL_CODEC_H
#define __PROTOCOL_CODEC_H

/* ============================================================================
 * protocol_codec.h —— 二进制帧编解码底层，唯一依据是 docs/protocol.md v1
 *
 * 本文件只做"与业务无关"的公共部分：协议常量、CRC-16/CCITT-FALSE、小端读写、
 * 通用帧封装（Begin/Finish/Build）。命令含义、设备状态、发送时机在
 * Hardware/Serial_Protocol.c 里，两者分工清晰。
 *
 * 所有多字节整数一律小端（LE）；帧内禁止出现任何文本/printf 调试输出。
 * ==========================================================================*/

#include "stm32f10x.h"   /* 提供 uint8_t / uint16_t / uint32_t */

/* ---- 帧常量（docs/protocol.md 第 1/2/3 节） ---- */
#define PROTO_VER               0x01u   /* VER，非 0x01 的帧丢弃并上报 */
#define PROTO_MAGIC0            0xA5u
#define PROTO_MAGIC1            0x5Au
#define PROTO_MAGIC2            0xC3u
#define PROTO_MAGIC3            0x3Cu
#define PROTO_TYPE_SAMPLE       0x10u   /* 设备 -> 电脑：采样帧 */
#define PROTO_TYPE_COMMAND      0x20u   /* 电脑 -> 设备：命令帧 */
#define PROTO_TYPE_RESPONSE     0x21u   /* 设备 -> 电脑：响应帧 */
#define PROTO_TYPE_EVENT        0x22u   /* 设备 -> 电脑：事件/错误帧 */
#define PROTO_HEADER_LEN        10u     /* 固定头 = MAGIC4+VER1+TYPE1+LEN2+SEQ1+FLAGS1 */
#define PROTO_CRC_LEN           2u      /* 帧尾 CRC16 */
#define PROTO_LEN_MAX           2060u   /* payload 上限 = 10 + 2*1024 */
#define PROTO_FRAME_MAX_LEN     (PROTO_LEN_MAX + PROTO_HEADER_LEN + PROTO_CRC_LEN) /* 2072 */
#define PROTO_RX_BUF_MAX        8192u   /* 接收缓冲硬上限 */
#define PROTO_FRAME_TIMEOUT_MS  1000u   /* 截断包 1s 恢复 */
#define PROTO_POINT_MAX         1024u   /* 每帧固定点数 */

/* ---- 命令参数范围（docs/protocol.md 第 4.2 节） ---- */
#define PROTO_RATE_10K          10000u
#define PROTO_RATE_100K         100000u
#define PROTO_VREF_MIN_MV       2500u
#define PROTO_VREF_MAX_MV       3600u
#define PROTO_VREF_DEFAULT_MV   3300u

/* ---- 命令 ID ---- */
#define CMD_START               0x01u
#define CMD_STOP                0x02u
#define CMD_SINGLE              0x03u
#define CMD_SET_RATE            0x04u
#define CMD_SET_VREF            0x05u
#define CMD_PING                0x06u
#define CMD_SELFTEST_PWM        0x07u
#define CMD_QUERY_STATUS        0x08u

/* ---- RESPONSE status（第 4.3 节） ---- */
#define STATUS_OK               0x00u
#define STATUS_BAD_PARAM        0x01u   /* 参数非法 */
#define STATUS_BUSY             0x02u   /* 忙，未切换 */
#define STATUS_UNSUPPORTED      0x03u
#define STATUS_HW_ERROR         0x04u

/* ---- RESPONSE run_state（第 4.3 节） ---- */
#define RUN_STATE_IDLE          0u
#define RUN_STATE_RUN           1u
#define RUN_STATE_SINGLE_BUSY   2u

/* ---- RESPONSE dev_flags（第 4.3 节） ---- */
#define DEV_FLAG_PWM_ON         0x01u   /* bit0 = 自检 PWM 已开启 */

/* ============================================================================
 * CRC-16/CCITT-FALSE（docs/protocol.md 第 5 节）
 *   poly 0x1021，初值 0xFFFF，输入/输出不反射，xorout 0，逐字节 MSB first。
 *   标准自检：crc16("123456789") == 0x29B1。
 *   覆盖范围：从 VER(偏移 4) 到 PAYLOAD 末，共 6+LEN 字节；帧内低字节在前。
 *
 * CRC_BIT1 完全照抄文档（纯整数表达式，本身不含 C 类型转换）。
 * CRC_STEP 保留文档"8 层 CRC_BIT1 嵌套、括号配平"的结构，只把起始处的
 *   ((uint16_t)(c)) / ((uint16_t)(b)) 两个 C 强制类型转换，
 *   换成数值等价的 ((c) & 0xFFFFu) / ((b) & 0xFFFFu)。
 *
 * 为什么必须这样改（这是与文档字面唯一的差异，务必看）：
 *   C 预处理器在 #if 中不允许 (uint16_t) 这类类型转换：未定义标识符
 *   uint16_t 会被替换成 0，(uint16_t)(c) 退化成 (0)(c) 是语法错误，
 *   编译期常量表达式断言无法求值，所以这里换成数值等价的 & 0xFFFFu。
 *
 * 【运行时与编译期的分工 —— 注意不要再把 CRC_STEP 写进运行时函数】
 *   CRC_STEP（8 层 CRC_BIT1 嵌套）只用于 Tests/protocol_selftest.c 的 #if
 *   编译期断言：预处理期求值，不生成任何代码。
 *   运行时 Protocol_Crc16（见 protocol_codec.c）是循环调用同一个 CRC_BIT1
 *   单步宏 8 次；若在运行时直接展开 8 层嵌套 CRC_STEP，ARMCC 会生成约
 *   110 KB 机器码，撑爆 STM32F103C8 的 64 KB Flash（本项目实测踩过这个坑）。
 *   两者都由同一个 CRC_BIT1 定义，语义完全一致。
 *
 * 等价性：(uint16_t) 的语义就是把值截断到低 16 位；CRC_BIT1 每步都 & 0xFFFFu；
 *   且调用时 crc 恒为 uint16_t(0..0xFFFF)、b 恒为 uint8_t(0..255)，
 *   因此 (c)&0xFFFFu 与 (uint16_t)(c) 数值完全相同，CRC 结果一字不差。
 *   四个 golden 值（0x29B1 / 0x8278 / 0x7EA4 / 0xE73A）由
 *   Tests/protocol_selftest.c 的 #if 断言用同一 CRC_STEP 独立证明。
 *
 * 下面这行是 8 层嵌套（由脚本生成并校验：括号差 0、CRC_BIT1 出现 8 次），
 * 写在一行是为了避免手工续行时把 8 层括号数错——这正是文档强调"括号平衡"的原因。
 * ==========================================================================*/
#define CRC_BIT1(c)  (((c) & 0x8000u) ? ((((c) << 1) ^ 0x1021u) & 0xFFFFu) : (((c) << 1) & 0xFFFFu))
#define CRC_STEP(c, b)  CRC_BIT1(CRC_BIT1(CRC_BIT1(CRC_BIT1(CRC_BIT1(CRC_BIT1(CRC_BIT1(CRC_BIT1(((((c) & 0xFFFFu) ^ (((b) & 0xFFFFu) << 8)))))))))))

/* ---- 底层 API ---- */
uint16_t Protocol_Crc16(const uint8_t *data, uint32_t len);

void     Protocol_WriteU16LE(uint8_t *p, uint16_t v);
void     Protocol_WriteU32LE(uint8_t *p, uint32_t v);
uint16_t Protocol_ReadU16LE(const uint8_t *p);
uint32_t Protocol_ReadU32LE(const uint8_t *p);

/* 帧封装：先 Begin（写固定头，返回 payload 区指针），填完 payload 再 Finish（写 CRC）。
 * Finish 返回整帧长度 = LEN + 12。这样 1024 点的采样帧可就地写进发送缓冲，
 * 无需再开一份 2058 字节的 payload 副本。 */
uint8_t *Protocol_BeginFrame(uint8_t *out, uint8_t type, uint8_t seq, uint16_t len);
uint16_t Protocol_FinishFrame(uint8_t *out, uint16_t len);
uint16_t Protocol_BuildFrame(uint8_t *out, uint8_t type, uint8_t seq,
                             const uint8_t *payload, uint16_t len);

#endif /* __PROTOCOL_CODEC_H */
