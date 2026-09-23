import { request } from "../request";

export interface XmStoreOption {
  store_id: string;
  store_name: string;
  store_no: string;
}

export interface XmStoreList {
  enabled: boolean;
  stores: XmStoreOption[];
  selected_store_id: string | null;
}

export const xmStoreApi = {
  list: (threadId?: string | null) =>
    request<XmStoreList>(
      `/xm-store/stores${
        threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ""
      }`,
    ),
  select: (storeId: string | null, threadId?: string | null) =>
    request<{ selected_store_id: string | null }>("/xm-store/selection", {
      method: "PUT",
      body: JSON.stringify({ store_id: storeId, thread_id: threadId ?? null }),
    }),
};
