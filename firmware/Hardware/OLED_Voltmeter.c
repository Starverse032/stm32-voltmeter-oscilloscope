#include "OLED_Voltmeter.h"
#include "OLED.h"
#include "protocol_codec.h"   /* RUN_STATE_*、DEV 常量 */

/* ============================================================================
 * OLED_Voltmeter.c —— 本地电压表（低刷新率）
 *
 * ---- 换算口径 ----
 *   协议第 4.1 节：V = code / 4095.0 * (vref_mv / 1000.0)。
 *   等价的整数形式：mV = code * vref_mv / 4095（code<=4095、vref_mv<=3600，
 *   乘积 <= 约 1.47e7，uint32 不溢出）。用 mV 显示，避免为 8 位机引入浮点库。
 *   OLED 与电脑端**必须**用同一个 vref_mv：这里由 main 把设备当前的 s_vref_mv
 *   传进来，采样帧里也填同一个值，因此两者标尺一致。
 *   不宣称板上 3.3V 恰好等于 3.300V：vref_mv 是可配置量（默认 3300），
 *   掉电不保存（只改 RAM），实际供电电压需现场测量后再校准。
 *
 * ---- 显示内容（每行长度固定，覆盖式刷新，不整屏 Clear，避免闪烁）----
 *   行1: ST:xxxx V:yyyy   (运行状态 IDLE/RUN /SNGL + 参考电压 mV)
 *   行2: AVG zzzz mV      (平均电压)
 *   行3: MIN zzzz mV      (最小电压)
 *   行4: MAX zzzz mV      (最大电压)
 *   状态串恒 4 字符、数字恒 4 位，保证每次写入长度一致、不会残留上一次字符。
 *
 * ---- 为什么刷新不破坏等间隔采样 ----
 *   采样是 TIM3+DMA 硬件完成的，本函数的 GPIO 软件 I2C bit-bang 只占 CPU，
 *   不会改变 TIM3 的 TRGO 周期。main.c 进一步只在 Scope_IsAcquiring()==0
 *   （帧间/空闲期）调用它，严格满足"刷新不能破坏一帧内的等间隔采样"。
 *   统计量是"最近一帧窗口"的，直流电压表用它稳定；交流信号的最小/最大
 *   只反映该帧窗口内的摆幅，与电脑端整帧统计口径一致。
 * ==========================================================================*/

void OLED_Voltmeter_Init(void)
{
    OLED_Init();
    OLED_Clear();
    /* 开机提示，随后被 ShowFrame 覆盖为固定宽度的 4 行数据 */
    OLED_ShowString(1, 1, "STM32 VOLTMETER");
    OLED_ShowString(2, 1, "ADC1 IN0 / PA0");
    OLED_ShowString(3, 1, "UART 115200 8N1");
    OLED_ShowString(4, 1, "V=code/4095*Vr");
}

void OLED_Voltmeter_ShowFrame(const uint16_t *buf, uint16_t n,
                              uint16_t vref_mv, uint8_t run_state)
{
    uint32_t sum = 0;
    uint16_t mn = 0xFFFFu, mx = 0u, code, i;
    uint32_t avg_code, avg_mv, min_mv, max_mv;
    char *st;

    if (n == 0u) { return; }

    for (i = 0; i < n; i++)
    {
        code = buf[i] & 0x0FFFu;          /* 12 位右对齐，屏蔽高位 */
        sum += code;
        if (code < mn) { mn = code; }
        if (code > mx) { mx = code; }
    }
    avg_code = sum / n;

    /* 整数 mV 换算，等价于协议的浮点公式 */
    avg_mv = (uint32_t)avg_code * vref_mv / 4095u;
    min_mv = (uint32_t)mn       * vref_mv / 4095u;
    max_mv = (uint32_t)mx       * vref_mv / 4095u;

    switch (run_state)
    {
        case RUN_STATE_RUN:          st = "RUN "; break;
        case RUN_STATE_SINGLE_BUSY:  st = "SNGL"; break;
        default:                     st = "IDLE"; break;
    }

    /* 行1：状态(4) + 参考电压(4)。所有可变字段长度固定。 */
    OLED_ShowString(1, 1, "ST:");
    OLED_ShowString(1, 4, st);
    OLED_ShowString(1, 8, " V:");
    OLED_ShowNum   (1, 11, vref_mv, 4);

    OLED_ShowString(2, 1, "AVG ");
    OLED_ShowNum   (2, 5, avg_mv, 4);
    OLED_ShowString(2, 9, " mV");

    OLED_ShowString(3, 1, "MIN ");
    OLED_ShowNum   (3, 5, min_mv, 4);
    OLED_ShowString(3, 9, " mV");

    OLED_ShowString(4, 1, "MAX ");
    OLED_ShowNum   (4, 5, max_mv, 4);
    OLED_ShowString(4, 9, " mV");
}
