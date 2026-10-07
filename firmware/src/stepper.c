/*
 * Motion engine - see stepper.h.
 *
 * The control law is mirrored in host/ptz/sim.py (simulator). Keep both in
 * sync when changing behaviour.
 *
 * Step generation (ISR, integer only):
 *   phase += inc each tick; an unsigned overflow emits one step.
 *   inc = |v| * 2^32 / STEP_TICK_HZ, capped at 2^31 - 1 so that a step can
 *   never be requested on two consecutive ticks (pulse width = 1 tick).
 *
 * Control (1 kHz, float):
 *   velocity mode : v -> v_cmd with vel_accel (max_accel when stopping)
 *   position mode : v_target = sign(err) * min(vmax, sqrt(2 * 0.9 * a * |err|))
 *   homing        : constant velocity until the ISR sees the endstop
 *   soft limits   : v_target clamped so the axis can always brake in time
 */
#include "stepper.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "config.h"
#include "hardware/gpio.h"
#include "hardware/structs/sio.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "pico/sync.h"
#include "pico/util/queue.h"

enum { MODE_IDLE, MODE_VELOCITY, MODE_POSITION, MODE_HOMING };

#define INC_PER_STEP_RATE (4294967296.0f / (float)STEP_TICK_HZ)
#define MAX_STEP_RATE     ((float)STEP_TICK_HZ / 2.0f)
#define BRAKE_MARGIN      0.9f

typedef struct {
    /* configuration */
    volatile bool configured;
    uint8_t  step_pin, dir_pin, enable_pin, endstop_pin;
    bool     dir_inv, en_inv, es_inv;
    int8_t   endstop_dir;
    uint32_t step_mask;
    float    max_vel, max_accel, vel_accel;

    /* shared with the ISR */
    volatile int32_t  pos;
    volatile uint32_t inc;
    volatile int8_t   dir;
    volatile int8_t   guard_dir;
    volatile bool     halted;
    volatile bool     triggered;
    volatile int32_t  trigger_pos;

    /* ISR private */
    uint32_t phase;
    int8_t   dir_state;
    bool     step_high;

    /* control loop private */
    bool    enabled, stopping, at_limit;
    uint8_t mode;
    float   v, v_cmd, move_vmax, move_accel, home_vel;
    int32_t target, home_start, home_max_travel;
    bool    lim_en;
    int32_t lim_min, lim_max;
} axis_t;

static axis_t axes[MAX_AXES];
static queue_t cmd_queue, event_queue;
static critical_section_t status_lock;
static msg_status_t status_snapshot;
static volatile uint32_t heartbeat;

static bool estop;
static uint32_t watchdog_ms = DEFAULT_WATCHDOG_MS;
static absolute_time_t last_feed;
static bool watchdog_expired;

/* ======================================================================
 * core 0 API
 * ====================================================================== */
static void clear_axes(void)
{
    memset(axes, 0, sizeof axes);
    for (int i = 0; i < MAX_AXES; i++)          /* 0 would be GPIO0 = host UART TX */
        axes[i].step_pin = axes[i].dir_pin = axes[i].enable_pin = axes[i].endstop_pin = PIN_NONE;
}

void stepper_init(void)
{
    clear_axes();
    queue_init(&cmd_queue, sizeof(stepper_cmd_t), CMD_QUEUE_DEPTH);
    queue_init(&event_queue, sizeof(msg_event_t), EVENT_QUEUE_DEPTH);
    critical_section_init(&status_lock);
    memset(&status_snapshot, 0, sizeof status_snapshot);
}

bool stepper_push(const stepper_cmd_t *cmd) { return queue_try_add(&cmd_queue, cmd); }
bool stepper_pop_event(msg_event_t *ev) { return queue_try_remove(&event_queue, ev); }
uint32_t stepper_heartbeat(void) { return heartbeat; }

void stepper_get_status(msg_status_t *st)
{
    critical_section_enter_blocking(&status_lock);
    *st = status_snapshot;
    critical_section_exit(&status_lock);
}

/* ======================================================================
 * step ISR (core 1, RAM)
 * ====================================================================== */
static bool __not_in_flash_func(step_isr)(repeating_timer_t *rt)
{
    (void)rt;
    uint32_t in = sio_hw->gpio_in;
    uint32_t set = 0, clr = 0;

    for (int i = 0; i < MAX_AXES; i++) {
        axis_t *a = &axes[i];
        if (!a->configured)
            continue;
        if (a->step_high) {                 /* end of the 1-tick pulse */
            clr |= a->step_mask;
            a->step_high = false;
        }
        uint32_t inc = a->inc;
        if (inc == 0 || a->halted)
            continue;
        int8_t dir = a->dir;
        if (dir != a->dir_state) {          /* dir setup time: one full tick */
            a->dir_state = dir;
            gpio_put(a->dir_pin, (dir > 0) ^ a->dir_inv);
            continue;
        }
        if (a->guard_dir != 0 && dir == a->guard_dir && a->endstop_pin != PIN_NONE) {
            bool es = ((in >> a->endstop_pin) & 1u) ^ a->es_inv;
            if (es) {
                a->halted = true;
                a->inc = 0;
                a->trigger_pos = a->pos;
                a->triggered = true;
                continue;
            }
        }
        uint32_t old = a->phase;
        a->phase = old + inc;
        if (a->phase < old) {               /* overflow -> one step */
            set |= a->step_mask;
            a->step_high = true;
            a->pos += dir;
        }
    }
    if (clr)
        sio_hw->gpio_clr = clr;
    if (set)
        sio_hw->gpio_set = set;
    return true;
}

/* ======================================================================
 * control loop helpers (core 1)
 * ====================================================================== */
static void push_event(uint8_t type, uint8_t axis, int32_t value)
{
    msg_event_t ev = { .type = type, .axis = axis, .value = value };
    queue_try_add(&event_queue, &ev);       /* drop if host is not reading */
}

static bool endstop_active(const axis_t *a)
{
    if (a->endstop_pin == PIN_NONE)
        return false;
    return gpio_get(a->endstop_pin) ^ a->es_inv;
}

static void apply_velocity(axis_t *a)
{
    float s = fabsf(a->v) * INC_PER_STEP_RATE;
    uint32_t inc = s >= 2147483647.0f ? 0x7FFFFFFFu : (uint32_t)s;
    if (inc)
        a->dir = a->v >= 0.0f ? 1 : -1;
    a->inc = inc;
}

static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

static void set_enable(axis_t *a, bool on)
{
    a->enabled = on;
    if (a->enable_pin != PIN_NONE)
        gpio_put(a->enable_pin, on ^ a->en_inv);
    if (!on) {
        a->mode = MODE_IDLE;
        a->v = a->v_cmd = 0.0f;
        apply_velocity(a);
    }
}

static void feed_watchdog(void)
{
    last_feed = get_absolute_time();
    watchdog_expired = false;
}

static void configure_axis(axis_t *a, const msg_config_axis_t *c)
{
    a->configured = false;                  /* ISR ignores the axis meanwhile */
    __dmb();
    a->step_pin = c->step_pin;
    a->dir_pin = c->dir_pin;
    a->enable_pin = c->enable_pin;
    a->endstop_pin = c->endstop_pin;
    a->dir_inv = c->flags & AXF_DIR_INVERT;
    a->en_inv = c->flags & AXF_ENABLE_INVERT;
    a->es_inv = c->flags & AXF_ENDSTOP_INVERT;
    a->endstop_dir = c->endstop_dir;
    a->step_mask = 1u << c->step_pin;
    a->max_vel = clampf(c->max_vel, 1.0f, MAX_STEP_RATE);
    a->max_accel = c->max_accel > 0 ? c->max_accel : 1.0f;
    a->vel_accel = (c->vel_accel > 0 && c->vel_accel <= a->max_accel) ? c->vel_accel : a->max_accel;

    gpio_init(a->step_pin);
    gpio_set_dir(a->step_pin, GPIO_OUT);
    gpio_put(a->step_pin, 0);
    gpio_init(a->dir_pin);
    gpio_set_dir(a->dir_pin, GPIO_OUT);
    gpio_put(a->dir_pin, a->dir_inv);
    if (a->enable_pin != PIN_NONE) {
        gpio_init(a->enable_pin);
        gpio_set_dir(a->enable_pin, GPIO_OUT);
    }
    if (a->endstop_pin != PIN_NONE) {
        gpio_init(a->endstop_pin);
        gpio_set_dir(a->endstop_pin, GPIO_IN);
        if (c->flags & AXF_ENDSTOP_PULLUP)
            gpio_pull_up(a->endstop_pin);
        else
            gpio_disable_pulls(a->endstop_pin);
    }
    a->pos = 0;
    a->phase = 0;
    a->inc = 0;
    a->dir = 1;
    a->dir_state = 0;
    a->halted = a->triggered = false;
    a->mode = MODE_IDLE;
    a->v = a->v_cmd = 0.0f;
    a->lim_en = false;
    set_enable(a, false);
    __dmb();
    a->configured = true;
}

static void reset_all(void)
{
    for (int i = 0; i < MAX_AXES; i++) {
        axis_t *a = &axes[i];
        if (a->configured) {
            set_enable(a, false);
            a->configured = false;
        }
    }
    __dmb();
    clear_axes();
    estop = false;
}

static void process_cmd(const stepper_cmd_t *c)
{
    axis_t *a = c->axis < MAX_AXES ? &axes[c->axis] : NULL;

    switch (c->type) {
    case CMD_RESET:
        reset_all();
        break;
    case CMD_CONFIG_AXIS:
        if (a)
            configure_axis(a, &c->cfg);
        break;
    case CMD_ENABLE:
        for (int i = 0; i < MAX_AXES; i++)
            if ((c->mask & (1u << i)) && axes[i].configured)
                set_enable(&axes[i], c->mask2 & (1u << i));
        break;
    case CMD_SET_VELOCITY:
        feed_watchdog();
        if (estop)
            break;
        for (int i = 0; i < MAX_AXES; i++) {
            axis_t *x = &axes[i];
            if (!(c->mask & (1u << i)) || !x->configured || !x->enabled || x->mode == MODE_HOMING)
                continue;
            x->mode = MODE_VELOCITY;
            x->v_cmd = clampf(c->f[i], -x->max_vel, x->max_vel);
            x->stopping = false;
        }
        break;
    case CMD_MOVE_TO:
        if (!a || !a->configured || !a->enabled || estop)
            break;
        a->mode = MODE_POSITION;
        a->target = c->i1;
        a->move_vmax = (c->f[0] > 0 && c->f[0] < a->max_vel) ? c->f[0] : a->max_vel;
        a->move_accel = (c->f[1] > 0 && c->f[1] < a->max_accel) ? c->f[1] : a->max_accel;
        break;
    case CMD_STOP:
        for (int i = 0; i < MAX_AXES; i++) {
            axis_t *x = &axes[i];
            if ((c->mask & (1u << i)) && x->configured && x->mode != MODE_IDLE) {
                x->mode = MODE_VELOCITY;
                x->v_cmd = 0.0f;
                x->stopping = true;
            }
        }
        break;
    case CMD_ESTOP:
        estop = true;
        for (int i = 0; i < MAX_AXES; i++) {
            axes[i].mode = MODE_IDLE;
            axes[i].v = axes[i].v_cmd = 0.0f;
            apply_velocity(&axes[i]);       /* drivers stay enabled (holding torque) */
        }
        break;
    case CMD_CLEAR_ESTOP:
        estop = false;
        break;
    case CMD_HOME:
        if (!a || !a->configured || !a->enabled || estop || a->endstop_pin == PIN_NONE)
            break;
        a->mode = MODE_HOMING;
        a->home_vel = clampf(c->f[0], -a->max_vel, a->max_vel);
        a->home_start = a->pos;
        a->home_max_travel = c->i1;
        a->halted = false;
        break;
    case CMD_SET_POSITION:
        if (!a || !a->configured)
            break;
        {
            uint32_t irq = save_and_disable_interrupts();   /* ISR writes pos */
            a->pos = c->i1;
            restore_interrupts(irq);
        }
        if (a->mode == MODE_POSITION)
            a->mode = MODE_IDLE;
        break;
    case CMD_SET_LIMITS:
        if (!a)
            break;
        a->lim_min = c->i1;
        a->lim_max = c->i2;
        a->lim_en = c->mask != 0;
        break;
    case CMD_SET_WATCHDOG:
        watchdog_ms = (uint32_t)c->i1;
        feed_watchdog();
        break;
    case CMD_FEED_WATCHDOG:
        feed_watchdog();
        break;
    }
}

static void check_watchdog(void)
{
    if (!watchdog_ms || watchdog_expired)
        return;
    if (absolute_time_diff_us(last_feed, get_absolute_time()) < (int64_t)watchdog_ms * 1000)
        return;
    for (int i = 0; i < MAX_AXES; i++) {
        if (axes[i].mode == MODE_VELOCITY && axes[i].v_cmd != 0.0f) {
            watchdog_expired = true;
            push_event(EV_WATCHDOG, 0, 0);
            return;
        }
    }
}

static void control_axis(uint8_t idx, axis_t *a, float dt)
{
    if (!a->configured)
        return;

    if (a->triggered) {                     /* ISR halted the axis on an endstop */
        a->triggered = false;
        a->v = a->v_cmd = 0.0f;
        push_event(a->mode == MODE_HOMING ? EV_HOME_TRIGGERED : EV_ENDSTOP_HIT, idx, a->trigger_pos);
        a->mode = MODE_IDLE;
    }
    if (estop || !a->enabled) {
        a->v = 0.0f;
        apply_velocity(a);
        return;
    }

    int32_t pos = a->pos;
    float acc = a->max_accel;
    float vt = 0.0f;

    switch (a->mode) {
    case MODE_VELOCITY:
        if (watchdog_expired) {
            a->v_cmd = 0.0f;
            a->stopping = true;
        }
        vt = a->v_cmd;
        acc = a->stopping ? a->max_accel : a->vel_accel;
        a->guard_dir = a->endstop_dir;
        break;
    case MODE_POSITION: {
        acc = a->move_accel;
        float err = (float)(a->target - pos);
        if (fabsf(err) < 1.0f && fabsf(a->v) <= acc * dt * 4.0f) {
            a->v = 0.0f;
            apply_velocity(a);
            a->mode = MODE_IDLE;
            push_event(EV_MOVE_DONE, idx, pos);
            return;
        }
        float vb = sqrtf(2.0f * BRAKE_MARGIN * acc * fabsf(err));
        vt = copysignf(fminf(a->move_vmax, vb), err);
        a->guard_dir = a->endstop_dir;
        break;
    }
    case MODE_HOMING:
        vt = a->home_vel;
        a->guard_dir = vt > 0.0f ? 1 : -1;
        if (abs(pos - a->home_start) > a->home_max_travel) {
            a->v = 0.0f;
            apply_velocity(a);
            a->mode = MODE_IDLE;
            push_event(EV_HOME_FAILED, idx, pos);
            return;
        }
        break;
    default:
        a->guard_dir = a->endstop_dir;
        break;
    }
    vt = clampf(vt, -a->max_vel, a->max_vel);

    /* soft limits: never command a speed we could not brake from in time */
    bool was_at_limit = a->at_limit;
    a->at_limit = false;
    if (a->lim_en && a->mode != MODE_HOMING) {
        float up = (float)(a->lim_max - pos), dn = (float)(pos - a->lim_min);
        float vup = up > 0 ? sqrtf(2.0f * BRAKE_MARGIN * acc * up) : 0.0f;
        float vdn = dn > 0 ? -sqrtf(2.0f * BRAKE_MARGIN * acc * dn) : 0.0f;
        if (vt > vup) { vt = vup; a->at_limit = true; }
        if (vt < vdn) { vt = vdn; a->at_limit = true; }
        if (a->at_limit && !was_at_limit)
            push_event(EV_LIMIT_HIT, idx, pos);
    }

    /* endstop guard: once halted, only allow motion away from the switch */
    if (a->halted) {
        if (vt * (float)a->guard_dir <= 0.0f)
            a->halted = false;
        else {
            vt = 0.0f;
            a->v = 0.0f;
        }
    }

    /* acceleration-limited ramp */
    float dv = acc * dt;
    a->v += clampf(vt - a->v, -dv, dv);
    if (a->lim_en && ((a->v > 0 && pos >= a->lim_max) || (a->v < 0 && pos <= a->lim_min)))
        a->v = 0.0f;
    apply_velocity(a);
}

static void publish_status(void)
{
    msg_status_t s;
    s.time_ms = to_ms_since_boot(get_absolute_time());
    s.sys_flags = (estop ? SYS_ESTOP : 0) | (watchdog_expired ? SYS_WATCHDOG : 0);
    for (int i = 0; i < MAX_AXES; i++) {
        axis_t *a = &axes[i];
        uint8_t f = 0;
        if (a->configured) {
            f |= ST_CONFIGURED;
            f |= a->enabled ? ST_ENABLED : 0;
            f |= (a->v != 0.0f || a->mode == MODE_POSITION || a->mode == MODE_HOMING) ? ST_MOVING : 0;
            f |= endstop_active(a) ? ST_ENDSTOP : 0;
            f |= a->mode == MODE_HOMING ? ST_HOMING : 0;
            f |= a->at_limit ? ST_AT_LIMIT : 0;
            f |= a->halted ? ST_HALTED : 0;
            f |= a->mode == MODE_POSITION ? ST_POSITION_MODE : 0;
        }
        s.axes[i].pos = a->pos;
        s.axes[i].vel = a->v;
        s.axes[i].flags = f;
    }
    critical_section_enter_blocking(&status_lock);
    status_snapshot = s;
    critical_section_exit(&status_lock);
}

/* ======================================================================
 * core 1 main
 * ====================================================================== */
void stepper_core1_main(void)
{
    /* Alarm pool created from core 1 => timer IRQ runs on core 1 */
    alarm_pool_t *pool = alarm_pool_create_with_unused_hardware_alarm(4);
    static repeating_timer_t timer;
    alarm_pool_add_repeating_timer_us(pool, -(int64_t)(1000000 / STEP_TICK_HZ),
                                      step_isr, NULL, &timer);

    const uint32_t period_us = 1000000 / CONTROL_HZ;
    const float dt = 1.0f / (float)CONTROL_HZ;
    absolute_time_t next = get_absolute_time();
    feed_watchdog();

    while (true) {
        next = delayed_by_us(next, period_us);
        stepper_cmd_t c;
        for (int n = 0; n < 16 && queue_try_remove(&cmd_queue, &c); n++)
            process_cmd(&c);
        check_watchdog();
        for (uint8_t i = 0; i < MAX_AXES; i++)
            control_axis(i, &axes[i], dt);
        publish_status();
        heartbeat++;
        busy_wait_until(next);
    }
}
