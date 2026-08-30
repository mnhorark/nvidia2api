"use client";

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

/**
 * localStorage 持久化 state：初值懒加载（首帧即是持久化值，无闪烁），
 * 之后每次变更自动写回。用于保存用户偏好（自动刷新、时间范围、思考档位、
 * 筛选条件等），刷新页面后不丢失。
 */
export function useLocalStorage<T>(
  key: string,
  initial: T
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    if (typeof window === "undefined") return initial;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return initial;
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* 忽略写入失败（隐私模式 / 配额已满等） */
    }
  }, [key, value]);

  return [value, setValue];
}
