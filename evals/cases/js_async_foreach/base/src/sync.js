'use strict';

const { openDb } = require('./db');

/**
 * Save every record, then close the connection.
 * Resolves with the number saved once all records are written.
 */
async function saveAll(records) {
  const db = await openDb();
  try {
    for (const record of records) {
      await db.insert('records', record);
    }
  } finally {
    await db.close();
  }
  return records.length;
}

module.exports = { saveAll };
