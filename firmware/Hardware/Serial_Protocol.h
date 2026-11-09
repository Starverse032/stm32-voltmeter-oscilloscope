#ifndef __SERIAL_PROTOCOL_H
#define __SERIAL_PROTOCOL_H

/* ============================================================================
 * Serial_Protocol.h —— USART1 二进制协议层（设备侧）
 *
 * 实现 docs/protocol.md 全部帧类型：
 *   0x20 COMMAND（收）、0x10 SAMPLE / 0x21 RESPONSE / 0x22 EVENT（发）。
 * 命令解析与响应、SEQ 回显、忙判定、接收字节流重同步状态机都在这里。
 *
 * 接口分两类：
 *   - 主循环每轮调用：Serial_Process(now_ms)
 *   - 采集状态机调用：Serial_SendSampleFrame() / Serial_GetXxx() / SetHwBusy()
 * ==========================================================================*/

#include "stm32f10x.h"

void     Serial_Protocol_Init(void);                       /* USART1 115200 8N1 + RX 中断 */
void     Serial_Process(uint32_t now_ms);                  /* 解析 FIFO、处理命令、发响应/EVENT、超时恢复 */
void     Serial_SendSampleFrame(const uint16_t *buf, uint16_t n); /* 发一帧 0x10，内部填真实 rate/vref/SEQ */

/* 设备状态（命令处理在本模块内改；采集调度在 main.c 里读/改少量状态） */
uint8_t  Serial_GetRunState(void);
void     Serial_SetRunState(uint8_t s);                    /* SINGLE 完成后由 main 置回 IDLE */
uint16_t Serial_GetVrefMv(void);                           /* 设备当前参考电压（掉电不保存） */
uint32_t Serial_GetRateHz(void);                           /* 直接返回 Scope 的真实生效值 */
void     Serial_SetHwBusy(uint8_t busy);                   /* main 通知"正在采集/发送一帧" */
uint8_t  Serial_IsBusy(void);                              /* SET_RATE 忙判定 */

#endif /* __SERIAL_PROTOCOL_H */
