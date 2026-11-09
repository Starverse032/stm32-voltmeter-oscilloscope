#ifndef __SELFTEST_PWM_H
#define __SELFTEST_PWM_H

/* ============================================================================
 * SelfTest_PWM.h —— PA8 上的 100Hz 自检 PWM（TIM1_CH1）
 *
 * 只是给用户一个"能看见信号在动"的自检源，用于验证接线和采集链路是否通，
 * 不能据此证明高速带宽或绝对精度（见 .c 注释）。
 * ==========================================================================*/

#include "stm32f10x.h"

void    SelfTest_PWM_Init(void);                 /* 配置好但默认关闭 */
void    SelfTest_PWM_Set(uint8_t on);            /* 0/1 开关，对应命令 0x07 */
void    SelfTest_PWM_SetDuty(uint16_t permille); /* 0..1000 -> 0..100% 占空比 */
uint8_t SelfTest_PWM_IsOn(void);                 /* 供 RESPONSE 的 dev_flags 位查询 */

#endif /* __SELFTEST_PWM_H */
