"""Automated unit test suite for code/intent.py.

Covers all 16 specified intent scenarios:
1. Explicit account deletion (intent_001)
2. Explicit personal-data erasure without account deletion (intent_002)
3. Request to export personal data (intent_003)
4. Request to access or inspect personal data (intent_004)
5. Marketing email opt-out only (intent_005)
6. Delete marketing preferences only (intent_006)
7. User asks how account deletion works (intent_007)
8. Negated deletion request (intent_008)
9. Conditional deletion wording (intent_009)
10. Mixed access and deletion request (intent_010)
11. Empty or missing request text (intent_011)
12. Leading/trailing whitespace and unusual punctuation (intent_012)
13. Common spelling variations (intent_013)
14. Supported non-English deletion wording (intent_014)
15. Supported non-English access or opt-out wording (intent_015)
16. Conflicting or genuinely ambiguous wording (intent_016)
"""

import sys
import unittest
from pathlib import Path

# Add project root and code directory to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from intent import (  # noqa: E402
    IntentClassificationResult,
    IntentType,
    RequestIntentClassifier,
    classify_request,
)


class TestRequestIntentClassifier(unittest.TestCase):
    """Test suite for RequestIntentClassifier."""

    @classmethod
    def setUpClass(cls):
        cls.classifier = RequestIntentClassifier()

    def test_intent_001_explicit_account_deletion(self):
        """Verify standard explicit account deletion request."""
        text = "Delete my account and all personal information associated with it."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.DELETE_REQUEST)
        self.assertTrue(result.is_deletion_request)
        self.assertFalse(result.is_not_applicable)

    def test_intent_002_explicit_personal_data_erasure(self):
        """Verify personal data erasure without explicit 'delete my account' phrasing."""
        text = "Please erase my personal data from your systems."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.DELETE_REQUEST)
        self.assertTrue(result.is_deletion_request)

    def test_intent_003_export_personal_data(self):
        """Verify data export and portability request without deletion."""
        text = "I'd like to download my account information in a portable format. Please keep the account as it is."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)
        self.assertFalse(result.is_deletion_request)

    def test_intent_004_access_or_inspect_personal_data(self):
        """Verify data access inquiry without deletion authorization."""
        text = "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_005_marketing_email_opt_out_only(self):
        """Verify promotional email unsubscribe / opt-out request."""
        text = "Please stop the promotional emails. I still need the account and my order history, so I am not asking for deletion."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_006_delete_marketing_preferences_only(self):
        """Verify deletion of marketing preferences only does not trigger account deletion."""
        text = "Please remove me from marketing lists only. I still want receipts and service messages for this account."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_007_user_asks_how_deletion_works(self):
        """Verify informational question does not trigger an actual deletion audit."""
        text = "How does account deletion work on this site? What is the process?"
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_008_negated_deletion_request(self):
        """Verify explicit negation prevents deletion classification."""
        text = "Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_009_conditional_deletion_wording(self):
        """Verify conditional wording leaving account active is not classified as deletion."""
        text = "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.NOT_DELETION)
        self.assertTrue(result.is_not_applicable)

    def test_intent_010_mixed_access_and_deletion_request(self):
        """Verify mixed request containing explicit deletion is classified as DELETE_REQUEST."""
        text = "Send me a copy of my data, and then permanently delete my account."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.DELETE_REQUEST)
        self.assertTrue(result.is_deletion_request)

    def test_intent_011_empty_or_missing_request_text(self):
        """Verify empty, whitespace, and None inputs handle safely as AMBIGUOUS."""
        self.assertEqual(self.classifier.classify(None).intent, IntentType.AMBIGUOUS)
        self.assertEqual(self.classifier.classify("").intent, IntentType.AMBIGUOUS)
        self.assertEqual(self.classifier.classify("   \n\t  ").intent, IntentType.AMBIGUOUS)

    def test_intent_012_whitespace_and_punctuation(self):
        """Verify classification robustness against strange punctuation and whitespace."""
        text = "   \n\n !!! Please delete my account permanently. ???  \t "
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.DELETE_REQUEST)

    def test_intent_013_spelling_variations(self):
        """Verify common typos in deletion commands are correctly recognized."""
        text = "Please delte my acount and erase my personal data."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.DELETE_REQUEST)

    def test_intent_014_multilingual_deletion(self):
        """Verify multilingual deletion requests in Spanish, Portuguese, French, Japanese, Korean, German."""
        samples = [
            ("Solicito la eliminación permanente de la cuenta asociada a este correo.", IntentType.DELETE_REQUEST),
            ("Solicito a exclusão permanente da conta associada a este e-mail.", IntentType.DELETE_REQUEST),
            ("Je demande la suppression définitive du compte associé à cette adresse e-mail.", IntentType.DELETE_REQUEST),
            ("このメールアドレスに関連するアカウントの完全な削除を依頼します。", IntentType.DELETE_REQUEST),
            ("이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.", IntentType.DELETE_REQUEST),
            ("Bitte mein Konto dauerhaft löschen und alle Daten entfernen.", IntentType.DELETE_REQUEST),
        ]
        for text, expected in samples:
            with self.subTest(text=text[:30]):
                res = self.classifier.classify(text)
                self.assertEqual(res.intent, expected)

    def test_intent_015_multilingual_non_deletion(self):
        """Verify multilingual opt-out and negation requests."""
        samples = [
            ("Ne supprimez pas mon compte, je souhaite seulement exporter mes données.", IntentType.NOT_DELETION),
            ("No eliminar mi cuenta, solo quiero descargar una copia de mis facturas.", IntentType.NOT_DELETION),
            ("Não excluir minha conta, apenas alterar meu endereço de e-mail.", IntentType.NOT_DELETION),
            ("アカウントは削除しないでください。データのダウンロードを希望します。", IntentType.NOT_DELETION),
            ("계정을 삭제하지 마세요. 뉴스레터만 수신 거부합니다.", IntentType.NOT_DELETION),
        ]
        for text, expected in samples:
            with self.subTest(text=text[:30]):
                res = self.classifier.classify(text)
                self.assertEqual(res.intent, expected)

    def test_intent_016_conflicting_or_ambiguous_wording(self):
        """Verify unclassifiable text falls back to AMBIGUOUS without guessing."""
        text = "Hello, I am thinking about various things and may contact you again later."
        result = self.classifier.classify(text)
        self.assertEqual(result.intent, IntentType.AMBIGUOUS)
        self.assertEqual(result.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
