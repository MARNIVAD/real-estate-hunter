import asyncio
import logging

from app.bot.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Bot terminated with an unhandled error")
        raise
