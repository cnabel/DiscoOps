#!/bin/bash

# DiscoOps Test Runner Script
# Run this locally to test the cog before pushing

set -e  # Exit on error

echo "🔧 DiscoOps Test Runner"
echo "======================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source venv/bin/activate

# Install/upgrade dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Run linting
echo -e "\n${YELLOW}Running code quality checks...${NC}"

echo "→ Checking with flake8..."
if flake8 cogs/discoops --max-line-length=127 --exclude=test_*.py; then
    echo -e "${GREEN}✓ Flake8 passed${NC}"
else
    echo -e "${RED}✗ Flake8 found issues${NC}"
fi

echo "→ Checking with black..."
if black --check cogs/discoops/ 2>/dev/null; then
    echo -e "${GREEN}✓ Black formatting check passed${NC}"
else
    echo -e "${YELLOW}! Code needs formatting (run: black cogs/discoops/)${NC}"
fi

echo "→ Checking imports with isort..."
if isort --check-only cogs/discoops/ 2>/dev/null; then
    echo -e "${GREEN}✓ Import sorting check passed${NC}"
else
    echo -e "${YELLOW}! Imports need sorting (run: isort cogs/discoops/)${NC}"
fi

# Run security check
echo -e "\n${YELLOW}Running security check...${NC}"
if bandit -r cogs/discoops/ -ll 2>/dev/null; then
    echo -e "${GREEN}✓ Security check passed${NC}"
else
    echo -e "${RED}✗ Security issues found${NC}"
fi

# Run tests
echo -e "\n${YELLOW}Running unit tests...${NC}"
cd cogs/discoops

if python -m pytest test_discoops.py -v --cov=discoops --cov-report=term-missing; then
    echo -e "\n${GREEN}✅ All tests passed!${NC}"
    exit_code=0
else
    echo -e "\n${RED}❌ Some tests failed${NC}"
    exit_code=1
fi

# Deactivate virtual environment
deactivate

# Summary
echo -e "\n${YELLOW}Test Summary:${NC}"
echo "============="
if [ $exit_code -eq 0 ]; then
    echo -e "${GREEN}All checks passed! Ready to commit.${NC}"
else
    echo -e "${RED}Some checks failed. Please fix issues before committing.${NC}"
fi

exit $exit_code
