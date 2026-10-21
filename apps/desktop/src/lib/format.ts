import dayjs from "dayjs";
import type { ApplicationSummary } from "../api/types";

/** Keyword → emoji so an application card reads at a glance, like the prototype. */
const EMOJI_RULES: [RegExp, string][] = [
  [/账|收支|财务|报销|预算|消费|花销/, "🌿"],
  [/客户|crm|销售|商机|会员|回访|联系人/, "🧺"],
  [/库存|物品|进销|仓库|商品|库存|台账|借还/, "📦"],
  [/订单|发货|物流|采购|供货/, "🧾"],
  [/学习|课程|单词|背词|读书|阅读|笔记|计划/, "📚"],
  [/任务|待办|项目|日程|清单/, "🗂"],
  [/健康|运动|健身|体重|饮食|睡眠|打卡/, "🥗"],
  [/旅行|行程|出游|游记/, "🧭"],
  [/宠物|猫|狗|领养/, "🐾"],
  [/菜谱|做饭|食谱|厨房/, "🍳"],
  [/客户|朋友|人脉|生日/, "🫂"],
  [/账单|订阅|续费/, "💳"],
  [/招聘|简历|面试|候选人/, "🧑‍💼"],
];

const EMOJI_POOL = ["🌱", "🪴", "🧩", "🗃", "🔖", "🧭", "🎯", "🌾", "🧷", "🪄"];

function hash(value: string): number {
  let result = 0;
  for (let index = 0; index < value.length; index += 1) {
    result = (result * 31 + value.charCodeAt(index)) | 0;
  }
  return Math.abs(result);
}

/** Pick a stable emoji for an application (prefers a keyword match). */
export function appEmoji(application: ApplicationSummary): string {
  if (application.icon && !/^[a-z0-9-]+$/i.test(application.icon)) return application.icon;
  const haystack = `${application.name}${application.description ?? ""}`;
  for (const [pattern, emoji] of EMOJI_RULES) {
    if (pattern.test(haystack)) return emoji;
  }
  return EMOJI_POOL[hash(application.name) % EMOJI_POOL.length];
}

/** "刚刚 / 12 分钟前 / 昨天 / 2026-10-20" — friendly recency label. */
export function relativeTime(value?: string | null): string {
  if (!value) return "";
  const moment = dayjs(value);
  if (!moment.isValid()) return "";
  const now = dayjs();
  const minutes = now.diff(moment, "minute");
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (moment.isSame(now, "day")) return moment.format("HH:mm");
  if (moment.isSame(now.subtract(1, "day"), "day")) return "昨天";
  const days = now.diff(moment, "day");
  if (days < 7) return `${days} 天前`;
  return moment.format("YYYY-MM-DD");
}

const WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"];
const MONTHS = [
  "JANUARY",
  "FEBRUARY",
  "MARCH",
  "APRIL",
  "MAY",
  "JUNE",
  "JULY",
  "AUGUST",
  "SEPTEMBER",
  "OCTOBER",
  "NOVEMBER",
  "DECEMBER",
];

/** "MONDAY · OCTOBER 20" — the editorial date line from the prototype. */
export function editorialDate(date = dayjs()): string {
  return `${WEEKDAYS[date.day() === 0 ? 6 : date.day() - 1]} · ${MONTHS[date.month()]} ${date.date()}`;
}

export function greeting(date = dayjs()): string {
  const hour = date.hour();
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 13) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

export function greetingEmoji(date = dayjs()): string {
  const hour = date.hour();
  if (hour < 5) return "🌙";
  if (hour < 11) return "☀️";
  if (hour < 18) return "🌤️";
  return "🌆";
}

/** Trim a path to the last segment for the workspace chip. */
export function workspaceName(path: string | undefined): string {
  if (!path) return "我的工作空间";
  const parts = path.split("/").filter(Boolean);
  const last = parts[parts.length - 1] ?? "";
  // ".workspace"-style dev dirs are implementation details, not a name.
  if (!last || last.startsWith(".")) return "我的工作空间";
  return last;
}
