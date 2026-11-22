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
  rpm_limit?: number | null;
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

// ------------------------------------------------------------------ members & invites

export type Member = Schemas["MemberView"];
export type Invite = Schemas["InviteView"];
export type InviteInput = Schemas["InviteCreate"];

export const qkm = {
  members: (id: string) => ["chats", id, "members"] as const,
  invites: (id: string) => ["chats", id, "invites"] as const,
  invitePreview: (token: string) => ["invite", token] as const,
};

export function useMembers(chatId: string) {
  return useQuery({
    queryKey: qkm.members(chatId),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}/members", path(chatId))),
  });
}

export function useInvites(chatId: string, enabled: boolean) {
  return useQuery({
    queryKey: qkm.invites(chatId),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}/invites", path(chatId))),
    enabled,
  });
}

export function useCreateInvite(chatId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: InviteInput) =>
      unwrap(api.POST("/chats/{chat_id}/invites", { ...path(chatId), body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qkm.invites(chatId) }),
  });
}

export function useRevokeInvite(chatId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) =>
      unwrap(api.DELETE("/invites/{invite_id}", { params: { path: { invite_id: inviteId } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qkm.invites(chatId) }),
  });
}

function useMemberWriter(chatId: string) {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: qkm.members(chatId) });
    qc.invalidateQueries({ queryKey: qk.chat(chatId) });
    qc.invalidateQueries({ queryKey: qk.chats, exact: true });
  };
}

export function useChangeRole(chatId: string) {
  const done = useMemberWriter(chatId);
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: "editor" | "viewer" }) =>
      unwrap(
        api.PATCH("/chats/{chat_id}/members/{user_id}", {
          params: { path: { chat_id: chatId, user_id: userId } },
          body: { role },
        }),
      ),
    onSettled: done,
  });
}

export function useRemoveMember(chatId: string) {
  const done = useMemberWriter(chatId);
  return useMutation({
    mutationFn: (userId: string) =>
      unwrap(
        api.DELETE("/chats/{chat_id}/members/{user_id}", {
          params: { path: { chat_id: chatId, user_id: userId } },
        }),
      ),
    onSettled: done,
  });
}

export function useInvitePreview(token: string) {
  return useQuery({
    queryKey: qkm.invitePreview(token),
    queryFn: () =>
      unwrap(api.GET("/invites/{token}/preview", { params: { path: { token } } })),
    retry: false,
  });
}

export function useAcceptInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) =>
      unwrap(api.POST("/invites/{token}/accept", { params: { path: { token } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.chats, exact: true }),
  });
}


// ------------------------------------------------------------------ turn controls
// Results arrive as live events (turn.cancelled, turn.retried, ...), so these
// mutations don't touch the query cache.

export function useCancelTurn(chatId: string) {
  return useMutation({
    mutationFn: (turnId: string) =>
      unwrap(
        api.POST("/chats/{chat_id}/turns/{turn_id}/cancel", {
          params: { path: { chat_id: chatId, turn_id: turnId } },
        }),
      ),
  });
}

export function useRetryTurn(chatId: string) {
  return useMutation({
    mutationFn: (turnId: string) =>
      unwrap(
        api.POST("/chats/{chat_id}/turns/{turn_id}/retry", {
          params: { path: { chat_id: chatId, turn_id: turnId } },
        }),
      ),
  });
}

export function useRegenerate(chatId: string) {
  return useMutation({
    mutationFn: (messageId: string) =>
      unwrap(
        api.POST("/chats/{chat_id}/messages/{message_id}/regenerate", {
          params: { path: { chat_id: chatId, message_id: messageId } },
        }),
      ),
  });
}

export function useDeletePending(chatId: string) {
  return useMutation({
    mutationFn: (messageId: string) =>
      unwrap(
        api.DELETE("/chats/{chat_id}/messages/{message_id}", {
          params: { path: { chat_id: chatId, message_id: messageId } },
        }),
      ),
  });
}

// ------------------------------------------------------------------ context & usage

export type ContextView = Schemas["ContextView"];
export type UsageView = Schemas["UsageView"];

export const qku = {
  context: (id: string) => ["chats", id, "context"] as const,
  usage: (id: string) => ["chats", id, "usage"] as const,
};

export function useContextInfo(chatId: string) {
  return useQuery({
    queryKey: qku.context(chatId),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}/context", path(chatId))),
  });
}

export function useUsage(chatId: string, enabled: boolean) {
  return useQuery({
    queryKey: qku.usage(chatId),
    queryFn: () => unwrap(api.GET("/chats/{chat_id}/usage", path(chatId))),
    enabled,
    refetchInterval: enabled ? 30_000 : false,
  });
}

export function useCompactNow(chatId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/chats/{chat_id}/context/compact", path(chatId))),
    onSettled: () => qc.invalidateQueries({ queryKey: qku.context(chatId) }),
  });
}

export function useClearSummary(chatId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.DELETE("/chats/{chat_id}/context/summary", path(chatId))),
    onSettled: () => qc.invalidateQueries({ queryKey: qku.context(chatId) }),
  });
}

export function useSetMemberCap(chatId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, cap }: { userId: string; cap: number | null }) =>
      unwrap(
        api.PATCH("/chats/{chat_id}/members/{user_id}", {
          params: { path: { chat_id: chatId, user_id: userId } },
          body: { member_token_cap: cap },
        }),
      ),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qku.usage(chatId) });
      qc.invalidateQueries({ queryKey: qkm.members(chatId) });
    },
  });
}

export function useAuthProviders() {
  return useQuery({
    queryKey: ["auth-providers"],
    queryFn: () => unwrap(api.GET("/auth/providers")),
    staleTime: Infinity,
  });
}

export function useDeleteAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.DELETE("/me")),
    onSuccess: () => {
      setCsrfToken(null);
      qc.clear();
      qc.setQueryData(qk.me, null);
    },
  });
}
