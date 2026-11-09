#include "protocol_codec.h"
/* 运行时用循环调用同一个 CRC_BIT1 单步宏，不展开 8 层嵌套 */
uint16_t Protocol_Crc16(const uint8_t *data, uint32_t len)
{
    uint16_t crc = 0xFFFFu;
    uint32_t n;
    uint8_t bit;
    for (n = 0; n < len; n++)
    {
        crc ^= (uint16_t)((uint16_t)data[n] << 8);
        for (bit = 0; bit < 8; bit++)
        {
            crc = (uint16_t)CRC_BIT1(crc);
        }
    }
    return crc;
}
