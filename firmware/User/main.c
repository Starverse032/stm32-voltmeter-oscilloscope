#include "stm32f10x.h"
#include "TimeBase.h"
#include "OLED_Voltmeter.h"
#include "ADC_Scope.h"
#include "Serial_Protocol.h"
#include "SelfTest_PWM.h"
#include "protocol_codec.h"

/* ============================================================================
 * main.c —— 总调度（保持简洁，细节都在各驱动里）
 *
 * 上电流程：初始化各模块 -> 进入主循环。**不等待电脑握手**，默认 IDLE 就周期性
 * 本地采集一帧喂给 OLED，因此没接电脑时设备独立作为电压表运行；串口命令只是
 * 附加控制通道。
 *
 * 主循环每轮做三件事（顺序固定）：
 *   1) Serial_Process(now)：处理串口命令、发 RESPONSE/EVENT、截断超时恢复；
 *   2) App_ScopeTask(now)：采集调度；
 *   3) OLED_Voltmeter_ShowFrame 在步骤 2 的"帧就绪之后"被调用。
 *
 * ---- 采集时序（任务要求的核心） ----
 *   采一帧 -> DMA TC 中断里立即停 TIM3/ADC 外部触发/DMA -> 置就绪标志 ->
 *   主循环把整帧发走（约 180ms）-> 发送完成后才重新配置并启动下一帧。
 *   发送期间 TIM3/ADC/DMA 全停，DMA 绝不覆盖正在读取的 g_adc_buf（单缓冲即安全，
 *   不需要双缓冲）。帧间空隙如实保留，不声称连续无损 100 kS/s 流。
 *
 * ---- 为什么 OLED 刷新不破坏一帧内的等间隔采样 ----
 *   采样由 TIM3 TRGO + DMA 全硬件完成，CPU 不逐点参与；主循环里的 OLED 刷新
 *   （软件 I2C bit-bang）和串口发送只占 CPU，不改变 TIM3 触发周期。
 *   唯一硬约束是"一帧 DMA 正在采集时不得重新配置 TIM3/ADC/DMA"——本工程把所有
 *   这类配置都放在 Scope_StartFrame()/Scope_Stop()（只在帧边界调用），并把 OLED
 *   刷新进一步安排在"帧就绪、DMA 已停"之后（Scope_IsAcquiring()==0 的帧间/空闲期），
 *   双重稳妥。volatile 与可见性的完整说明见 ADC_Scope.c 文件头。
 *
 * ---- IDLE 与 STOP 的语义（值得说明的设计取舍） ----
 *   IDLE（含初始上电、STOP 之后）仍以约 200ms 周期本地采集一帧，**仅供 OLED
 *   电压表读数，不向串口发送采样帧**。这样既满足"无电脑连接独立电压表"，
 *   也满足"STOP 停止连续采集发帧"。RUN 状态才连续采一帧发一帧。
 *
 * ---- 采样率 ----
 *   默认 10 kS/s；SET_RATE 可切 100 kS/s。100 kS/s 是设计目标，不是已上板实测指标。
 * ==========================================================================*/

/* App 层采集调度状态（只在本文件访问，无需 volatile）：
 * 设备逻辑状态(run_state/vref 等)在 Serial_Protocol.c，硬件采集在 ADC_Scope.c。 */
enum { ACQ_IDLE_WAIT = 0, ACQ_ACQUIRING = 1 };
static uint8_t  s_acq_phase            = ACQ_IDLE_WAIT;
static uint8_t  s_frame_for_send       = 0;   /* 当前帧是否发到串口 */
static uint8_t  s_return_idle_after    = 0;   /* 这一帧发完后是否把 SINGLE_BUSY 置回 IDLE */
static uint32_t s_idle_next_ms         = 0;   /* IDLE 下一帧最早时间 */

static void App_ScopeTask(uint32_t now)
{
    switch (s_acq_phase)
    {
        case ACQ_IDLE_WAIT:
            if (Serial_GetRunState() == RUN_STATE_RUN)
            {
                /* 连续采集：发一帧 -> 停 -> 立刻发下一帧 */
                s_frame_for_send    = 1;
                s_return_idle_after = 0;
                Serial_SetHwBusy(1);
                Scope_StartFrame();
                s_acq_phase = ACQ_ACQUIRING;
            }
            else if (Serial_GetRunState() == RUN_STATE_SINGLE_BUSY)
            {
                /* 单次：采发 1 帧，完成后回 IDLE */
                s_frame_for_send    = 1;
                s_return_idle_after = 1;
                Serial_SetHwBusy(1);
                Scope_StartFrame();
                s_acq_phase = ACQ_ACQUIRING;
            }
            else
            {
                /* IDLE：周期性本地采集一帧，仅刷新 OLED，不发串口 */
                if ((int32_t)(now - s_idle_next_ms) >= 0)
                {
                    s_frame_for_send    = 0;
                    s_return_idle_after = 0;
                    Serial_SetHwBusy(1);
                    Scope_StartFrame();
                    s_acq_phase = ACQ_ACQUIRING;
                }
            }
            break;

        case ACQ_ACQUIRING:
            if (Scope_IsFrameReady())
            {
                /* 此刻 DMA ISR 已执行 Scope_Stop()：TIM3/ADC 触发/DMA 全停，
                   g_acquiring=0，缓冲有效且不会再被覆盖 -> 这是"帧间/空闲期"。 */

                /* 先刷新 OLED（本地电压表永远可用），刷新本身不改 TIM/ADC/DMA 配置。 */
                OLED_Voltmeter_ShowFrame(Scope_GetBuffer(),
                                         Scope_GetPointCount(),
                                         Serial_GetVrefMv(),
                                         Serial_GetRunState());

                /* 再按需发送整帧；发送完成后函数才返回，之后才允许启动下一帧。 */
                if (s_frame_for_send)
                {
                    Serial_SendSampleFrame(Scope_GetBuffer(), Scope_GetPointCount());
                }

                Scope_ClearFrameReady();
                Serial_SetHwBusy(0);
                s_acq_phase     = ACQ_IDLE_WAIT;
                s_idle_next_ms  = now + 200u;   /* IDLE 时下一帧至少 200ms 后；RUN 忽略 */

                if (s_return_idle_after)
                {
                    Serial_SetRunState(RUN_STATE_IDLE);   /* SINGLE 完成回 IDLE */
                    s_return_idle_after = 0;
                }
            }
            break;

        default:
            s_acq_phase = ACQ_IDLE_WAIT;
            break;
    }
}

int main(void)
{
    /* 优先级分组 2（2 位抢占 + 2 位子优先级）：
       DMA1_Channel1=抢占0、USART1=抢占1、SysTick=最低。 */
    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);

    TimeBase_Init();          /* SysTick 1ms 时基 */
    OLED_Voltmeter_Init();    /* OLED 软件 I2C（PB8/PB9） */
    ADC_Scope_Init();         /* ADC1/TIM3/DMA/NVIC，只初始化不启动 */
    Serial_Protocol_Init();   /* USART1 115200 8N1 + RXNE 中断 */
    SelfTest_PWM_Init();      /* PA8 100Hz 自检 PWM，默认关闭 */

    /* 上电即开始本地电压表采集，不等待电脑握手。 */
    s_acq_phase     = ACQ_IDLE_WAIT;
    s_idle_next_ms  = TimeBase_Ms();

    while (1)
    {
        uint32_t now = TimeBase_Ms();
        /* 命令仅在上一帧处理完后执行。否则 SINGLE 采集中收到 START，
         * 其 RUN 状态会在当前单次帧结束时被错误覆盖为 IDLE；也可能让
         * SINGLE 的 ACK 先于上一帧发出，使电脑误把旧帧认作新单次结果。
         * 采集/发送期间 RX ISR 继续入 FIFO，帧间再按顺序处理。 */
        if (s_acq_phase == ACQ_IDLE_WAIT)
        {
            Serial_Process(now);
        }
        App_ScopeTask(now);      /* 采一帧 -> 停 -> 发一帧 -> 才启动下一帧 */
    }
}
