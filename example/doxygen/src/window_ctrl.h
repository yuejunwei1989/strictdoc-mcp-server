/**
 * @file window_ctrl.h
 * @brief 车窗控制组件对外接口
 * @ingroup win_control
 */

/**
 * @brief 防夹触发后输出反向控制指令
 *
 * 检测到防夹条件后，按 100 ms 内的时限输出反向指令；
 * 位置回退到触发点下方 5 cm 处停止。
 *
 * @req SWR-0002
 * @param position_cm 当前车窗位置，单位 cm，有效范围 0-60
 * @param dir_px 反向运动方向脉冲数
 * @return 0 成功；-1 驱动故障
 */
int window_ctrl_reverse(int position_cm, int dir_px);

/**
 * @brief 车窗位置采样
 *
 * @req SWR-0001
 * @param raw_adc ADC 原始采样值
 * @return 换算后的位置，单位 cm
 */
int window_ctrl_sample_position(int raw_adc);
