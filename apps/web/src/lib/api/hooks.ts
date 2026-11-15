"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  setCsrfToken,
  unwrap,
  type Chat,
  type Me,
  type Schemas,
} from "./client";

export const qk = {
  me: ["me"] as const,
  chats: ["chats"] as const,
  chat: (id: string) => ["chats", id] as const,
  messages: (id: string) => ["chats", id, "messages"] as const,
  models: (id: string) => ["chats", id, "models"] as const,
  providers: ["providers"] as const,
};

const path = (chat_id: string) => ({ params: { path: { chat_id } } });

export function useMe() {
  return useQuery<Me | null>({
    queryKey: qk.me,
    queryFn: async () => {
      try {
        const me = await unwrap(api.GET("/me"));
        setCsrfToken(me.csrf_token);
        return me;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setCsrfToken(null);
          return null;
        }
        throw e;
      }
    },
    staleTime: 5 * 60_000,
  });
}

export function useDevLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; name: string }) =>
      unwrap(api.POST("/auth/dev/login", { body })),
    onSuccess: (me) => {
      setCsrfToken(me.csrf_token);
      qc.clear();
      qc.setQueryData(qk.me, me);
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/auth/logout")),
    onSettled: () => {
      setCsrfToken(null);
      qc.clear();
      qc.setQueryData(qk.me, null);
    },
  });
}

export function useChats(enabled = true) {
  return useQuery({ queryKey: qk.chats, queryFn: () => unwrap(api.GET("/chats")), enabled });
}

export function useChat(id: string) {
  return useQuery({
    queryKey: qk.chat(id),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}", path(id))),
    retry: (n, e) => !(e instanceof ApiError && [403, 404].includes(e.status)) && n < 2,
  });
}

export function useProviders() {
  return useQuery({
    queryKey: qk.providers,
    queryFn: () => unwrap(api.GET("/providers")),
    staleTime: Infinity,
  });
}

export function useModels(id: string, enabled: boolean) {
  return useQuery({
    queryKey: qk.models(id),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}/models", path(id))),
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

function useChatWriter() {
  const qc = useQueryClient();
  return (chat: Chat) => {
    qc.setQueryData(qk.chat(chat.id), chat);
    qc.invalidateQueries({ queryKey: qk.chats, exact: true });
  };
}

export function useCreateChat() {
  const write = useChatWriter();
  return useMutation({
    mutationFn: (title?: string) => unwrap(api.POST("/chats", { body: { title } })),
    onSuccess: write,
  });
}

export type ChatPatch = {
  title?: string;
  custom_instructions?: string;
  model?: string;
  max_output_tokens?: number;
  daily_token_cap?: number | null;
};

export function usePatchChat(id: string) {
  const write = useChatWriter();
  return useMutation({
    mutationFn: (body: ChatPatch) =>
      unwrap(api.PATCH("/chats/{chat_id}", { ...path(id), body })),
    onSuccess: write,
  });
}

export function useDeleteChat() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/chats/{chat_id}", path(id))),
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: qk.chat(id) });
      qc.invalidateQueries({ queryKey: qk.chats, exact: true });
    },
  });
}

function useKeyMutation<A, R>(id: string, fn: (arg: A) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.chat(id) });
      qc.invalidateQueries({ queryKey: qk.chats, exact: true });
      qc.removeQueries({ queryKey: qk.models(id) });
    },
  });
}

export type KeyInput = Schemas["KeyIn"];

export function usePutKey(id: string) {
  return useKeyMutation(id, (body: KeyInput) =>
    unwrap(api.PUT("/chats/{chat_id}/key", { ...path(id), body })),
  );
}

export function useDeleteKey(id: string) {
  return useKeyMutation(id, () => unwrap(api.DELETE("/chats/{chat_id}/key", path(id))));
}

export function useValidateKey(id: string) {
  return useKeyMutation(id, () => unwrap(api.POST("/chats/{chat_id}/key/validate", path(id))));
}
