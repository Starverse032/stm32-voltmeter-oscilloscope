#ifndef __OLED_VOLTMETER_H
#define __OLED_VOLTMETER_H

/* ============================================================================
 * OLED_Voltmeter.h —— 本地电压表显示
 *
 * 沿用参考工程的软件 I2C OLED（SCL=PB8、SDA=PB9，OLED.c/OLED.h/OLED_Font.h），
 * 这里只做"把一帧 ADC 数据换算成电压并显示"的业务层。
 *
 * 刷新时机由 main.c 安排（只在非 DMA 采集的帧间/空闲期调用），本模块不自己定时。
 * 设备不等待电脑握手：上电后 main 会周期性本地采集一帧喂给本模块，
 * 因此没接电脑时 OLED 也独立作为电压表运行。
 * ==========================================================================*/

#include "stm32f10x.h"

void OLED_Voltmeter_Init(void);

/* 用一帧数据刷新显示：平均/最小/最大电压 + 运行状态 + 参考电压。
 * 换算口径与协议一致：V = code/4095.0 * (vref_mv/1000.0)。
 * 这里用整数 mV 运算 mV = code*vref_mv/4095，数值等价且避免引入浮点。 */
void OLED_Voltmeter_ShowFrame(const uint16_t *buf, uint16_t n,
                              uint16_t vref_mv, uint8_t run_state);

#endif /* __OLED_VOLTMETER_H */
