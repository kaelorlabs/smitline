// Imported first by each entry point, so COLLEAGUE_* variables are in place under their
// SMITLINE_* names before any module reads its settings.
import { adoptOldSettings } from './old-names.mjs';

adoptOldSettings(process.env);
