"use client";

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

/**
 * localStorage 持久化 state：初值懒加载（首帧即是持久化值，无闪烁），
 * 之后每次变更自动写回。用于保存用户偏好（自动刷新、时间范围、思考档位、
 * 筛选条件等），刷新页面后不丢失。
 */
/**
 * 读取本 hook 持久化的值（与写入格式一致，会做 JSON 反序列化）。
 *
 * 直接 `localStorage.getItem(key)` 拿到的是**带引号的 JSON 串**
 * （例如 `"gpt-4o"`），拿去当月筛选条件会把脏值发给后端。
 */
export function readStoredValue<T>(key: string, initial: T): T {
  if (typeof window === "undefined") return initial;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return initial;
    return JSON.parse(raw) as T;
  } catch {
    return initial;
  }
}


export function useLocalStorage<T>(
  key: string,
  initial: T
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => readStoredValue(key, initial));

  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* 忽略写入失败（隐私模式 / 配额已满等） */
    }
  }, [key, value]);

  return [value, setValue];
}
