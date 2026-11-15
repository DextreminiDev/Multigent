import { StatusBadge } from "./status-badge";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col justify-center gap-4 px-4">
      <h1 className="text-3xl font-semibold tracking-tight">Multigent</h1>
      <p className="text-muted">
        Shared, real-time AI chats. The owner brings the key; everyone talks to the same model.
      </p>
      <StatusBadge />
    </main>
  );
}
