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
  list: (threadId?: string | null, agentId?: string | null) => {
    const params = new URLSearchParams();
    if (threadId) params.set("thread_id", threadId);
    if (agentId) params.set("agent_id", agentId);
    return request<XmStoreList>(
      `/xm-store/stores${params.size ? `?${params}` : ""}`,
    );
  },
  select: (
    storeId: string | null,
    threadId?: string | null,
    agentId?: string | null,
  ) =>
    request<{ selected_store_id: string | null }>("/xm-store/selection", {
      method: "PUT",
      body: JSON.stringify({
        store_id: storeId,
        thread_id: threadId ?? null,
        agent_id: agentId ?? null,
      }),
    }),
};
