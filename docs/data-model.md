# Data Model

This is the persistence layer behind everything the user does in the app —
every wardrobe item, outfit, chat, post, and message the frontend shows is
backed by one of these collections. MongoDB (`smartwardrobe` database), 13
collections. Indexes are (re)created
idempotently on every `web` startup (`app/core/database.py::_ensure_indexes`).
Outfits reference wardrobe items by id rather than embedding a snapshot, so
an outfit always reflects the current state of its items — the one
deliberate exception is a shared post's frozen snapshot (see below).

| Collection | Purpose | Key indexes |
|---|---|---|
| `users` | Account credentials (bcrypt-hashed password) | unique `username`, unique `email` |
| `wardrobe` | Clothing items: name, `clothing_type`, color, season, style, occasion, optional image/tags | `user_id`; compound `user_id`+ each of type/season/style/occasion/color |
| `outfits` | Saved outfits: `item_ids` (references into `wardrobe`), optional name/occasion/season/style | compound `user_id`+`item_ids` |
| `feedback` | Rating (1–5) / liked / comment on an already-saved outfit | unique `user_id`+`outfit_id` |
| `recommendation_feedback` | Like/dislike on a fresh, unsaved recommendation, with structured reason scope | `user_id`-scoped |
| `memory` | One long-term preference profile per user: preferred styles, liked/disliked colors, disliked item types, preferred occasions, fit, communication style, notes | unique `user_id` |
| `chat_jobs` | Asynchronous chat job queue: prompt, status (pending/processing/completed/failed), result/error_reason, `attempts` (claim count; also the claim-fencing token `complete_job`/`fail_job` require to match before resolving a job, so a stale claim from before a reclaim can never resolve a newer one) | compound `status`+`priority`+`created_at` (queue-claim order); compound `status`+`started_at` (stale-job recovery scan) |
| `posts` | Forum posts, optional anonymity (real author kept internally only), optional shared-outfit snapshot | `created_at` desc; compound `author_id`+`created_at` |
| `comments` | Flat, non-anonymous comments on a post | compound `post_id`+`created_at` |
| `reactions` | Like/dislike on a post or comment; one document per user per target | compound `user_id`+`target_type`+`target_id`; compound `target_type`+`target_id`+`value` |
| `conversations` | One-to-one DM conversation, keyed by an unordered user-pair | unique `pair_key` |
| `direct_messages` | Individual DM messages within a conversation, optional image/MP4 video attachment | conversation/time-ordered |
| `notifications` | Recipient-scoped notifications for new DMs and post/comment reactions | compound `recipient_id`+`created_at`; compound `recipient_id`+`read` |

## Notable model-layer guarantees

- **Enums round-trip safely.** `ClothingType`, `Season`, `Occasion`,
  `Style`, `FitPreference`, `CommunicationStyle` are all `str`-mixin Enums,
  consistent everywhere they're used (wardrobe, outfits, recommendations,
  memory) and stored/read from BSON without translation.
- **Response models reject unlisted fields.** This is a deliberate
  hallucination-robustness measure (see `architecture.md`): a memory note
  can never leak into or override a wardrobe item's actual response shape.
- **`SharedOutfitSnapshot` is intentionally the one place data is frozen,
  not referenced** — categorical attributes only (clothing_type/color/
  season/style/occasion per component), never a wardrobe item id, name,
  image, or owner reference, so a shared post can be recreated by anyone
  without ever exposing the source wardrobe.
