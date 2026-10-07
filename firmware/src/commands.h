#pragma once
#include <stddef.h>
#include <stdint.h>

extern uint32_t g_status_period_us;

void commands_handle(uint8_t id, uint8_t seq, const uint8_t *payload, size_t len);
