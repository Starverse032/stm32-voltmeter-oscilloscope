#include "TimeBase.h"
#include "system_stm32f10x.h"   /* SystemCoreClock = 72000000 */

/* s_ms 由 SysTick 中断写、主循环读，必须 volatile：否则编译器可能把主循环里
 * 的读取优化成"只读一次寄存器"，导致永远看不到时间前进。 */
static volatile uint32_t s_ms = 0;

void TimeBase_Init(void)
{
    /* SystemCoreClock = 72MHz，72000 个计数 = 1ms。
     * SysTick_Config 会开 SysTick 中断并把它的 NVIC 优先级设为最低，
     * 因此 DMA(抢占0) / USART(抢占1) 都能抢占它，不影响"采满立即停 TIM"。 */
    SysTick_Config(SystemCoreClock / 1000u);
}

uint32_t TimeBase_Ms(void)
{
    return s_ms;
}

void TimeBase_Tick(void)
{
    s_ms++;
}
