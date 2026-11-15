"use client";

import { useParams } from "next/navigation";
import { ChatScreen } from "@/features/chat/chat-screen";

export default function ChatPage() {
  const { chatId } = useParams<{ chatId: string }>();
  return <ChatScreen key={chatId} chatId={chatId} />;
}
