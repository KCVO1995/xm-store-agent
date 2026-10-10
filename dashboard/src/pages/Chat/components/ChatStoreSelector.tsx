import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { App, Button, Select } from "antd";

import { xmStoreApi, type XmStoreList } from "../../../api/modules/xmStore";
import { isPendingThread } from "../hooks/useSessions";

interface ChatStoreSelectorProps {
  agentId?: string | null;
  threadId?: string | null;
  isStreaming: boolean;
}

export default function ChatStoreSelector({
  agentId,
  threadId,
  isStreaming,
}: ChatStoreSelectorProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const persistedThreadId =
    threadId && !isPendingThread(threadId) ? threadId : null;
  const [storeList, setStoreList] = useState<XmStoreList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    setStoreList(null);
    setLoading(true);
    setError(false);
    void xmStoreApi
      .list(persistedThreadId, agentId)
      .then((next) => {
        if (!cancelled) setStoreList(next);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, persistedThreadId, retry]);

  const selectStore = async (storeId: string | null) => {
    try {
      await xmStoreApi.select(storeId, persistedThreadId, agentId);
      setStoreList((current) =>
        current ? { ...current, selected_store_id: storeId } : current,
      );
    } catch {
      message.error(t("chat.storeSelectFailed", "门店选择失败，请重试"));
    }
  };

  if (!storeList?.enabled && !error && !loading) return null;
  return (
    <>
      <span
        style={{
          whiteSpace: "nowrap",
          fontSize: 12,
          color: "var(--fn-text-tertiary)",
        }}
      >
        {t("chat.storeSelector", "门店")}
      </span>
      {error ? (
        <Button size="small" onClick={() => setRetry((value) => value + 1)}>
          {t("chat.storeLoadRetry", "门店加载失败，重试")}
        </Button>
      ) : (
        <Select
          showSearch
          allowClear
          loading={loading}
          disabled={loading || isStreaming || !storeList?.stores.length}
          style={{ minWidth: 180, maxWidth: "100%" }}
          placeholder={t("chat.storePlaceholder", "选择对话门店")}
          value={storeList?.selected_store_id ?? undefined}
          optionFilterProp="label"
          options={storeList?.stores.map((store) => ({
            value: store.store_id,
            label: `${store.store_name} (${store.store_no})`,
          }))}
          onChange={(value) => void selectStore(value ?? null)}
        />
      )}
    </>
  );
}
