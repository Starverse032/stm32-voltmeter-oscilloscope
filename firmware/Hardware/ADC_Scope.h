#ifndef __ADC_SCOPE_H
#define __ADC_SCOPE_H

/* ============================================================================
 * ADC_Scope.h —— 示波器采集端接口（ADC1_IN0 + TIM3 硬件触发 + DMA1_Channel1）
 *
 * 对外只暴露"启动一帧 / 是否就绪 / 取缓冲 / 改采样率"等少量函数，
 * 让 main.c 保持简洁。所有 TIM/ADC/DMA 配置细节都封装在 ADC_Scope.c 内。
 * ==========================================================================*/

#include "stm32f10x.h"

#define SCOPE_POINT_COUNT  1024u   /* 第一版固定每帧 1024 点 */

/* 初始化 ADC1/TIM3/DMA/NVIC，但不启动采集（等 Scope_StartFrame）。 */
void ADC_Scope_Init(void);

/* 只接受 10000 / 100000 Hz；返回 1=接受并生效，0=拒绝。
 * 必须在"没有正在采集的一帧"时调用（调用方用忙判定保证），否则会改坏当帧。 */
uint8_t  Scope_SetRateHz(uint32_t hz);
uint32_t Scope_GetRateHz(void);       /* 返回当前真实生效的采样率 */

/* 启动一帧：重置 DMA、使能 ADC 外部触发、启动 TIM3。非阻塞，立即返回。 */
void Scope_StartFrame(void);

/* 停止 TIM3 / ADC 外部触发 / DMA。DMA 传输完成中断里调用，也可主循环调用。 */
void Scope_Stop(void);

/* DMA 完成中断与主循环之间的状态标志（volatile，见 .c 注释）。 */
uint8_t  Scope_IsFrameReady(void);
void     Scope_ClearFrameReady(void);
uint8_t  Scope_IsAcquiring(void);      /* 1 = 正在 DMA 采集一帧 */

/* 只有就绪标志为 1 且 DMA 已停时，才能安全读这块缓冲。 */
const uint16_t *Scope_GetBuffer(void);
uint16_t        Scope_GetPointCount(void);

#endif /* __ADC_SCOPE_H */
