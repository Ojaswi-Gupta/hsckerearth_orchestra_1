"""Request Intent Classification Engine for Ghost Data Still Exists.

Distinguishes genuine personal-data deletion requests from non-deletion requests
(e.g., data access, export/portability, marketing opt-out, profile corrections,
login support, or general inquiries) and explicit negations across multiple languages.

Output contract for downstream pipeline:
- DELETE_REQUEST: Proceed with identity resolution and deletion auditing.
- NOT_DELETION: Output official status NOT_APPLICABLE with steps_json = [].
- AMBIGUOUS: Flagged for conservative handling and evaluation diagnostics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class IntentType(Enum):
    """Categorization of user request intent."""
    DELETE_REQUEST = "DELETE_REQUEST"
    NOT_DELETION = "NOT_DELETION"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class IntentClassificationResult:
    """Detailed result of request intent classification."""
    intent: IntentType
    reason: str
    matched_signals: List[str] = field(default_factory=list)
    classification_method: str = "rule_based"
    confidence: float = 1.0

    @property
    def is_deletion_request(self) -> bool:
        """True if the request authorizes personal data deletion."""
        return self.intent == IntentType.DELETE_REQUEST

    @property
    def is_not_applicable(self) -> bool:
        """True if the request does not authorize deletion (downstream NOT_APPLICABLE)."""
        return self.intent == IntentType.NOT_DELETION


class RequestIntentClassifier:
    """Deterministic, rule-based multilingual request intent classifier."""

    # 1. Explicit Negation Patterns: Requester specifically states NOT to delete
    NEGATION_PATTERNS = [
        # English
        (r"\bnot\s+(?:asking\s+for|requesting)\s+(?:deletion|to\s+erase|to\s+delete)\b", "negation_not_asking_deletion"),
        (r"\bdo\s+not\s+(?:delete|erase|close|remove)\s+(?:my\s+|the\s+)?account\b", "negation_do_not_delete_account"),
        (r"\bdon[\'’]t\s+(?:delete|erase|close|remove)\s+(?:my\s+|the\s+)?account\b", "negation_dont_delete_account"),
        (r"\bdon[\'’]t\s+(?:delete\s+or\s+change|change)\s+anything\b", "negation_dont_delete_or_change"),
        (r"\bwithout\s+(?:removing|deleting|erasing)\s+(?:any\s+of\s+my|my)\s+(?:records|data|account)\b", "negation_without_removing"),
        (r"\bleave\s+(?:the|my)\s+account\s+(?:active|open|as\s+it\s+is|in\s+place)\b", "negation_leave_account_active"),
        (r"\bkeep\s+(?:the|my)\s+account\s+(?:as\s+it\s+is|active|open)?\b", "negation_keep_account"),
        (r"\bcontinue\s+using\s+the\s+service\b", "negation_continue_using_service"),
        (r"\bnot\s+a\s+request\s+to\s+(?:erase|delete)\b", "negation_not_request_to_erase"),
        (r"\bdo\s+not\s+close\s+the\s+account\b", "negation_do_not_close_account"),
        # French
        (r"\bne\s+pas\s+(?:supprimer|effacer|clôturer)\s+(?:mon\s+|le\s+)?compte\b", "fr_negation_ne_pas_supprimer"),
        (r"\bne\s+supprimez\s+pas\s+(?:mon\s+|le\s+)?compte\b", "fr_negation_ne_supprimez_pas"),
        # Spanish / Portuguese
        (r"\bno\s+(?:eliminar|borrar)\s+(?:mi\s+|la\s+)?cuenta\b", "es_negation_no_eliminar"),
        (r"\bno\s+(?:eliminen|borren)\s+(?:mi\s+|la\s+)?cuenta\b", "es_negation_no_eliminen"),
        (r"\bnão\s+(?:excluir|apagar)\s+(?:minha\s+|a\s+)?conta\b", "pt_negation_nao_excluir"),
        # German
        (r"\b(?:nicht\s+l[oö]schen|keine\s+L[oö]schung)\b", "de_negation_nicht_loeschen"),
        # Japanese / Korean
        (r"削除しないで", "ja_negation_do_not_delete"),
        (r"삭제하지\s*마세요", "ko_negation_do_not_delete"),
    ]

    # 2. Informational Inquiry Patterns: Asking how deletion works without requesting it
    INQUIRY_PATTERNS = [
        (r"\bhow\s+(?:do\s+i|can\s+i|does)\s+.*(?:delet|clos|eras)", "inquiry_how_to_delete"),
        (r"\bwhat\s+is\s+(?:the\s+process|your\s+policy)\s+(?:for|to)\s+(?:delet|eras|clos)", "inquiry_what_is_process"),
        (r"\bcomment\s+supprimer\s+un\s+compte\b", "fr_inquiry_how_to_delete"),
        (r"\bc[oó]mo\s+se\s+elimina\s+una\s+cuenta\b", "es_inquiry_how_to_delete"),
    ]

    # 3. Pure Non-Deletion Subject Matter: Access, Export, Marketing Opt-Out, Corrections
    NON_DELETION_PATTERNS = [
        # Marketing opt-out only
        (r"\bmarketing\s+lists?\s+only\b", "opt_out_marketing_lists_only"),
        (r"\bstop\s+(?:the\s+)?promotional\s+emails?\b", "opt_out_stop_promotional_emails"),
        (r"\bmarketing\s+preferences\s+only\b", "opt_out_marketing_preferences_only"),
        (r"\bunsubscribe\s+(?:me\s+)?from\s+(?:promotional|marketing|all\s+emails)\b", "opt_out_unsubscribe"),
        # Data access and inspection
        (r"\bwhat\s+personal\s+information\s+you\s+(?:hold|store)\s+about\s+me\b", "access_what_info_held"),
        (r"\btell\s+me\s+what\s+personal\s+information\s+you\s+store\b", "access_tell_me_info"),
        (r"\bcopy\s+of\s+(?:the\s+|last\s+year[\'’]s\s+)?invoices?\b", "access_copy_of_invoices"),
        (r"\bsend\s+me\s+a\s+copy\s+of\s+(?:my\s+|the\s+)?(?:personal\s+)?data\b", "access_send_copy_of_data"),
        (r"\bcopy\s+of\s+the\s+information\s+held\s+on\s+my\s+account\b", "access_copy_of_info_held"),
        # Data export / portability without deletion
        (r"\bdownload\s+my\s+account\s+information\b", "export_download_account_info"),
        (r"\bexport\s+(?:my\s+)?(?:account\s+)?(?:information|data)\b", "export_account_data"),
        # Profile correction / support
        (r"\b(?:surname|name|phone|mobile|email)\s+(?:is\s+)?misspelled\b", "rectification_misspelled"),
        (r"\breplace\s+it\s+with\s+the\s+new\s+one\b", "rectification_replace_field"),
        (r"\bchange\s+the\s+email\s+address\s+used\s+for\s+this\s+account\b", "rectification_change_email"),
        (r"\bresetting\s+the\s+login\b", "support_reset_login"),
        (r"\bpassword\s+reset\b", "support_password_reset"),
    ]

    # 4. Genuine Deletion Patterns: Account closure, permanent deletion, personal data erasure
    DELETION_PATTERNS = [
        # English: Explicit account deletion & closure
        (r"\b(?:permanently\s+)?delete\s+(?:the|my|this)?\s*account\b", "en_delete_account"),
        (r"\bdeletion\s+of\s+(?:the|my|this)?\s*account\b", "en_deletion_of_account"),
        (r"\baccount\s+(?:associated\s+with\s+this\s+address\s+)?to\s+be\s+erased\b", "en_account_to_be_erased"),
        (r"\b(?:account|it)\s+permanently\s+deleted\b", "en_account_permanently_deleted"),
        (r"\b(?:permanent\s+)?account[- ]deletion\b", "en_account_deletion"),
        (r"\bdeletion\s+process\s+for\s+the\s+account\b", "en_deletion_process_for_account"),
        (r"\bclose\s+(?:the|my|this)?\s*account(?:\s+completely)?\b", "en_close_account"),
        (r"\berase\s+(?:the|my)?\s*account\b", "en_erase_account"),
        (r"\bremove\s+(?:the|my|this)?\s*account\b", "en_remove_account"),
        (r"\bno\s+longer\s+need\s+this\s+account\b", "en_no_longer_need_account"),
        # English: Personal data erasure & de-identification
        (r"\bpersonal[- ]data\s+deletion\b", "en_personal_data_deletion"),
        (r"\berase\s+(?:my|the)?\s*personal\s+data\b", "en_erase_personal_data"),
        (r"\bremove\s+(?:my|the)?\s*personal\s+(?:information|details)\b", "en_remove_personal_info"),
        (r"\bremove\s+(?:my\s+)?name\s+and\s+contact\s+details\b", "en_remove_name_contact_details"),
        (r"\bremove\s+records(?:\s+linked\s+to|\s+under)?\b", "en_remove_records"),
        (r"\bhave\s+its\s+associated\s+personal\s+data\s+deleted\b", "en_associated_data_deleted"),
        (r"\bhave\s+it\s+closed\b", "en_have_it_closed"),
        (r"\bdelet(?:e|ing)\s+my\s+account\b", "en_deleting_my_account"),
        (r"\bbefore\s+deleting\s+(?:it|the\s+account|my\s+account)\b", "en_before_deleting_it"),
        (r"\bremove\s+(?:the|my)\s+profile\b", "en_remove_profile"),
        (r"\bdelete\s+that\s+export\s+file\b", "en_delete_export_file"),
        # English: Post-deletion verification & audit inquiries
        (r"\bdeleted\s+(?:this|my)\s+account\b", "en_deleted_account_audit"),
        (r"\baudit\s+the\s+deletion\b", "en_audit_the_deletion"),
        (r"\bverify\s+that\s+my\s+account\s+was\s+deleted\b", "en_verify_account_deleted"),
        (r"\btold\s+(?:that\s+)?deletion\s+was\s+complete\b", "en_told_deletion_complete"),
        (r"\btold\s+the\s+account\s+had\s+been\s+deleted\b", "en_told_account_deleted"),
        (r"\bfinish\s+the\s+deletion\b", "en_finish_the_deletion"),
        (r"\bconfirm\s+that\s+the\s+profile\s+is\s+gone\b", "en_confirm_profile_gone"),
        (r"\bprofile\s+is\s+gone\b", "en_profile_is_gone"),
        (r"\brequest\s+failed.*inspect\s+the\s+actual\s+systems\b", "en_request_failed_inspect"),
        (r"\baccount\s+page\s+is\s+gone\b", "en_account_page_gone"),
        (r"\baccount\s+itself\s+is\s+gone\b", "en_account_itself_gone"),
        # Spanish: Eliminación de cuenta
        (r"eliminaci[oó]n\s+permanente\s+de\s+la\s+cuenta", "es_eliminacion_permanente_cuenta"),
        (r"eliminar\s+mi\s+cuenta", "es_eliminar_mi_cuenta"),
        (r"cerrar\s+mi\s+cuenta", "es_cerrar_mi_cuenta"),
        # Portuguese: Exclusão de conta
        (r"exclus[aã]o\s+permanente\s+da\s+conta", "pt_exclusao_permanente_conta"),
        (r"excluir\s+minha\s+conta", "pt_excluir_minha_conta"),
        (r"eliminar\s+minha\s+conta", "pt_eliminar_minha_conta"),
        # French: Suppression de compte
        (r"suppression\s+d[eé]finitive\s+du\s+compte", "fr_suppression_definitive_compte"),
        (r"supprimer\s+mon\s+compte", "fr_supprimer_mon_compte"),
        (r"cl[oô]turer\s+mon\s+compte", "fr_cloturer_mon_compte"),
        # Japanese: アカウント削除
        (r"アカウントの完全な削除", "ja_account_complete_deletion"),
        (r"アカウント.*削除", "ja_account_deletion"),
        # Korean: 계정 삭제
        (r"계정의\s+영구\s+삭제", "ko_account_permanent_deletion"),
        (r"계정.*삭제", "ko_account_deletion"),
        # German: Konto löschen
        (r"Konto\s+(?:dauerhaft\s+|endg[uü]ltig\s+)?l[oö]schen", "de_konto_loeschen"),
        (r"dauerhafte\s+L[oö]schung", "de_dauerhafte_loeschung"),
    ]

    # Spelling variation tolerant patterns
    TYPO_TOLERANT_PATTERNS = [
        (r"\b(?:delte|delet|dlete)\s+(?:my\s+|the\s+)?(?:acount|accout|account)\b", "typo_delete_account"),
        (r"\beras\s+(?:my\s+|the\s+)?(?:acount|accout|account|personal\s+data)\b", "typo_erase_account"),
    ]

    def classify(self, request_text: Optional[str]) -> IntentClassificationResult:
        """Classify a request text into DELETE_REQUEST, NOT_DELETION, or AMBIGUOUS."""
        if request_text is None:
            return IntentClassificationResult(
                intent=IntentType.AMBIGUOUS,
                reason="Request text is None.",
                matched_signals=["input_none"],
                confidence=0.0,
            )

        cleaned_text = request_text.strip()
        if not cleaned_text:
            return IntentClassificationResult(
                intent=IntentType.AMBIGUOUS,
                reason="Request text is empty or blank whitespace.",
                matched_signals=["input_empty"],
                confidence=0.0,
            )

        text_lower = cleaned_text.lower()

        # Step 1: Check for explicit negation of deletion
        negation_matches = []
        for pattern, sig_name in self.NEGATION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                negation_matches.append(sig_name)

        # Step 2: Check for informational inquiries
        inquiry_matches = []
        for pattern, sig_name in self.INQUIRY_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                inquiry_matches.append(sig_name)

        # Step 3: Check for pure non-deletion topics (access, export, opt-out, rectification)
        non_del_matches = []
        for pattern, sig_name in self.NON_DELETION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                non_del_matches.append(sig_name)

        # Step 4: Check for positive deletion instructions
        del_matches = []
        for pattern, sig_name in self.DELETION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                del_matches.append(sig_name)

        # Check typo-tolerant patterns if no standard deletion pattern matched
        if not del_matches:
            for pattern, sig_name in self.TYPO_TOLERANT_PATTERNS:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    del_matches.append(sig_name)

        # Decision Logic:
        # A. Explicit negation present
        if negation_matches:
            # If user explicitly said "do not delete" or "not asking for deletion",
            # this overrides secondary mentions of deletion words
            return IntentClassificationResult(
                intent=IntentType.NOT_DELETION,
                reason=f"Explicit negation of deletion matched: {', '.join(negation_matches)}",
                matched_signals=negation_matches,
                confidence=0.98,
            )

        # B. Informational inquiries without an overriding imperative deletion command
        if inquiry_matches:
            # Check if there is an explicit separate command to delete right now
            has_imperative_command = bool(
                re.search(r"\b(?:please|pls|i\s+want\s+to|i\s+demand|kindly)\s+(?:delete|erase|close|remove)\b", text_lower)
            )
            if not has_imperative_command:
                return IntentClassificationResult(
                    intent=IntentType.NOT_DELETION,
                    reason=f"Informational inquiry without deletion instruction: {', '.join(inquiry_matches)}",
                    matched_signals=inquiry_matches,
                    confidence=0.95,
                )

        # C. Pure non-deletion (access, export, opt-out, correction) without deletion command
        if non_del_matches and not del_matches:
            return IntentClassificationResult(
                intent=IntentType.NOT_DELETION,
                reason=f"Non-deletion subject matter matched: {', '.join(non_del_matches)}",
                matched_signals=non_del_matches,
                confidence=0.95,
            )

        # D. Mixed request: Access/Export AND explicit deletion
        if non_del_matches and del_matches:
            # User wants data export AND account deletion -> Valid deletion request
            return IntentClassificationResult(
                intent=IntentType.DELETE_REQUEST,
                reason=f"Mixed request containing explicit deletion command: {', '.join(del_matches)}",
                matched_signals=del_matches + non_del_matches,
                confidence=0.90,
            )

        # E. Explicit deletion request
        if del_matches:
            return IntentClassificationResult(
                intent=IntentType.DELETE_REQUEST,
                reason=f"Explicit personal data/account deletion command: {', '.join(del_matches)}",
                matched_signals=del_matches,
                confidence=0.99,
            )

        # F. Fallback: Ambiguous / Unclassifiable
        return IntentClassificationResult(
            intent=IntentType.AMBIGUOUS,
            reason="No known deletion or non-deletion intent signals could be matched.",
            matched_signals=["unmatched"],
            confidence=0.0,
        )


# Module-level instance and convenience function
_DEFAULT_CLASSIFIER = RequestIntentClassifier()


def classify_request(request_text: Optional[str]) -> IntentClassificationResult:
    """Classify a request text into DELETE_REQUEST, NOT_DELETION, or AMBIGUOUS.
    
    Args:
        request_text: The user's input request message.
        
    Returns:
        IntentClassificationResult with intent enum, explanation reason, and matched signals.
    """
    return _DEFAULT_CLASSIFIER.classify(request_text)
