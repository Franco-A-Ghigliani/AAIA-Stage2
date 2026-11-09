BEGIN TRANSACTION;
CREATE TABLE actions (seq INTEGER PRIMARY KEY, time REAL, turn INTEGER, action TEXT, status TEXT,
                      result TEXT);
INSERT INTO "actions" VALUES(1,1.794233453218021392e+09,2,'{"tool": "list_files", "args": {}}','uncertain',NULL);
CREATE TABLE checkpoints (seq INTEGER PRIMARY KEY, time REAL, turn INTEGER, tree TEXT, state TEXT);
INSERT INTO "checkpoints" VALUES(1,1.794233453218021392e+09,2,'original-tree','{"turn": 2, "messages": [], "counts": {"model_calls": 2}}');
CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id = 1), spec TEXT, status TEXT, pid INTEGER,
                   reason TEXT, result TEXT);
INSERT INTO "meta" VALUES(1,'{"request": "unchanged task", "limits": {"max_actions": 4}}','created',NULL,NULL,NULL);
COMMIT;
