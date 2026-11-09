#ifndef __TIMEBASE_H
#define __TIMEBASE_H

/* ============================================================================
 * TimeBase.h —— 毫秒时基（SysTick 1ms 中断）
 *
 * 用途：OLED 低刷新率调度、串口截断包 1s 超时恢复。
 * 为什么用 SysTick 中断而不是 Delay_ms 死等：主循环不能被阻塞，否则串口命令
 * 和采样状态机都会被拖住。SysTick 只维护一个 volatile 毫秒计数，开销极小。
 * 该时基不参与采样触发（采样由 TIM3 硬件完成），见 ADC_Scope.c 的说明。
 * ==========================================================================*/

#include "stm32f10x.h"

void     TimeBase_Init(void);
uint32_t TimeBase_Ms(void);   /* 返回上电以来的毫秒数（volatile，ISR 更新） */
void     TimeBase_Tick(void); /* 仅由 SysTick_Handler 调用，每 1ms +1 */

#endif /* __TIMEBASE_H */
