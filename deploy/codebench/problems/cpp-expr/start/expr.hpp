#pragma once
#include <string>

// Evaluates an arithmetic expression and returns its value.
//
// Grammar (whitespace may appear between any two tokens):
//
//   expr   := term (('+' | '-') term)*
//   term   := unary (('*' | '/') unary)*
//   unary  := '-' unary | power
//   power  := atom ('^' unary)?
//   atom   := number | '(' expr ')'
//   number := digit+ ('.' digit+)?
//
// So '+', '-', '*' and '/' are left-associative, '^' is right-associative
// and binds tighter than unary minus: -2^2 is -4, 2^3^2 is 512, 2^-1 is 0.5.
//
// Errors:
//   std::invalid_argument  the text does not match the grammar: empty input,
//                          an unknown character, a missing operand,
//                          unbalanced parentheses, or anything left over
//                          after a complete expression
//   std::domain_error      division by zero
double evaluate(const std::string& text);
