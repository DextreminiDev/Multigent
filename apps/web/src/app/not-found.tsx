import Link from "next/link";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main id="main" className="grid min-h-dvh place-items-center px-4 text-center">
      <div className="max-w-sm space-y-4">
        <Logo className="text-lg" />
        <h1 className="text-xl font-semibold">Page not found</h1>
        <p className="text-sm text-muted-foreground">
          This page doesn&apos;t exist. If someone sent you an invite, check that the whole link was
          copied.
        </p>
        <Button render={<Link href="/" />}>Go to your chats</Button>
      </div>
    </main>
  );
}
