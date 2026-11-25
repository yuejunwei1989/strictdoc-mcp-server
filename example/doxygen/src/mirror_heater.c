/**
 * @file mirror_heater.c
 * @brief 后视镜加热控制
 */

/**
 * @defgroup win_control 车窗控制组件
 * @brief 负责车窗升降、防夹与位置反馈
 * @{
 */

/**
 * @brief 加热使能判断
 *
 * @req SWR-0003
 * @param ambient_temp_c 环境温度，单位摄氏度
 * @return 1 使能加热；0 关闭
 */
int mirror_heater_enable(int ambient_temp_c);

/** @} */
