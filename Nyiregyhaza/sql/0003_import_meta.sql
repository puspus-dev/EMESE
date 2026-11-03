-- Az importer ide jegyzi meg, melyik feed-verziót töltötte be utoljára (ETag, Last-Modified, sha256)
create table if not exists nyiregyhaza.import_meta (
  key   text primary key,
  value text not null
);
