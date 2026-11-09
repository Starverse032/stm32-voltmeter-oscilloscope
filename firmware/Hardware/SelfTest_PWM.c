#include "SelfTest_PWM.h"

/* ============================================================================
 * SelfTest_PWM.c —— PA8 = TIM1_CH1，100Hz、0~3.3V 可变占空比 PWM
 *
 * ---- 频率推导 ----
 *   TIM1 挂在 APB2，时钟 = 72MHz（APB2 预分频为 1）。
 *   PSC = 720-1 = 719  -> 计数频率 = 72MHz / 720 = 100kHz（每数 10us）。
 *   ARR = 1000-1 = 999 -> 100kHz / 1000 = 100Hz（周期 10ms）。
 *   CCR(Pulse) 0..1000 对应 0..100% 占空比；改变占空比即改变 PA8 方波的
 *   平均电压（0~3.3V），可作为一路可变的自检信号接回 PA0 观察。
 *
 * ---- 局限（务必如实告知） ----
 *   1) 这只是一个 100Hz 的 GPIO 方波，用来验证"接线 + ADC/DMA 采集链路"是否通，
 *      **不能**据此证明放大/模拟带宽、高频响应，也**不能**证明绝对电压精度。
 *   2) PA8 输出是 0~3.3V 的数字方波，不是线性模拟电压；若要观察"模拟量"应
 *      经 RC 滤波，且滤波后的值与占空比、电源实际电压都有关。
 *   3) 自检输出与外部信号**不可同时驱动 PA0**：两者都往 PA0 灌电压会冲突，
 *      甚至损坏引脚。使用自检时请断开外部输入，只把 PA8（经限流/滤波）接到 PA0。
 *   4) 板上 3.3V 不恰好等于 3.300V，PWM 高电平的实际幅值待现场测量，不写成实测值。
 *
 * ---- 引脚冲突 ----
 *   PA8 默认复用 = TIM1_CH1；也复用为 USART1_CK / MCO。本工程 USART1 只作
 *   异步收发（PA9/PA10），不使用 CK，也不使用 MCO -> 不冲突。
 *   TIM1 的 CH2/CH3 分别在 PA9/PA10，但本工程只启用 CH1，PA9/PA10 留给
 *   USART1 -> 不冲突。详见 firmware/引脚资源核对.md。
 *
 * ---- 默认关闭 ----
 *   TIM1 是高级定时器，PWM 主输出由 MOE 位(TIM_CtrlPWMOutputs)统一开关。
 *   初始化后计数器可运行，但 MOE=0（关闭主输出），PA8 不输出波形；
 *   收到命令 0x07 打开时才 ENABLE。dev_flags bit0 反映此状态。
 * ==========================================================================*/

static uint8_t s_on = 0;

void SelfTest_PWM_Init(void)
{
    GPIO_InitTypeDef        gpio;
    TIM_TimeBaseInitTypeDef tim;
    TIM_OCInitTypeDef       oc;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_TIM1 |
                           RCC_APB2Periph_AFIO, ENABLE);

    gpio.GPIO_Pin   = GPIO_Pin_8;
    gpio.GPIO_Mode  = GPIO_Mode_AF_PP;      /* 复用推挽：电平交给 TIM1_CH1 */
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &gpio);

    tim.TIM_Period            = 999u;       /* ARR = 1000-1 */
    tim.TIM_Prescaler         = 719u;       /* PSC = 720-1 */
    tim.TIM_ClockDivision     = TIM_CKD_DIV1;
    tim.TIM_CounterMode       = TIM_CounterMode_Up;
    tim.TIM_RepetitionCounter = 0u;
    TIM_TimeBaseInit(TIM1, &tim);

    /* OCStructInit 先把高级定时器才有的 N 通道/空闲状态字段填成安全默认值，
       再覆盖我们要用的 CH1 字段。 */
    TIM_OCStructInit(&oc);
    oc.TIM_OCMode      = TIM_OCMode_PWM1;           /* CNT < CCR 输出高 */
    oc.TIM_OutputState = TIM_OutputState_Enable;
    oc.TIM_OCPolarity  = TIM_OCPolarity_High;
    oc.TIM_Pulse       = 500u;                       /* 默认 50% 占空比 */
    TIM_OC1Init(TIM1, &oc);
    TIM_OC1PreloadConfig(TIM1, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(TIM1, ENABLE);

    TIM_CtrlPWMOutputs(TIM1, DISABLE);               /* 默认关闭主输出 */
    TIM_Cmd(TIM1, ENABLE);                           /* 计数器跑，但 MOE=0 不输出 */
    s_on = 0;
}

void SelfTest_PWM_Set(uint8_t on)
{
    if (on)
    {
        TIM_CtrlPWMOutputs(TIM1, ENABLE);
        s_on = 1;
    }
    else
    {
        TIM_CtrlPWMOutputs(TIM1, DISABLE);
        s_on = 0;
    }
}

void SelfTest_PWM_SetDuty(uint16_t permille)
{
    if (permille > 1000u) { permille = 1000u; }
    TIM_SetCompare1(TIM1, permille);
}

uint8_t SelfTest_PWM_IsOn(void)
{
    return s_on;
}
